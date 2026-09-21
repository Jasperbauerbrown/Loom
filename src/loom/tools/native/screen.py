"""The `screenshot` tool (03 §9.2): a picture of the screen, a monitor, a window or a
region, returned as an image the model can look at.

No dependency is added for it. Windows captures through PowerShell's System.Drawing
(`CopyFromScreen`), run non-interactively with no window of its own; macOS uses
`screencapture`; Linux uses the first of `grim`, `gnome-screenshot`, `import`
(ImageMagick) or `scrot` it finds. Nothing is brought to the foreground. The capture
lands in a temp file under the conversation's artifact folder that is read and removed;
the dispatcher then stores the bytes as an ordinary image artifact (03 §4.4).

`capture(spec)` is the one seam: tests replace it, the platforms implement it.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ... import prompts
from ...artifacts import image_dimensions, sniff_image
from ...ids import ulid
from ..registry import Tool, ToolContext, ToolResult

CAPTURE_TIMEOUT_S = 25.0
FORMATS = ("png", "jpeg")


class CaptureError(Exception):
    pass


@dataclass
class CaptureSpec:
    out_path: str
    monitor: int = 1  # 1 = primary, 2… = the others; 0 = the whole virtual screen
    region: dict[str, int] | None = None  # {x, y, width, height} in virtual-screen pixels
    window: str = ""  # substring of a top-level window title (Windows)
    scale: float = 1.0
    fmt: str = "png"

    def describe(self) -> str:
        if self.window:
            return f"window '{self.window}'"
        if self.region:
            r = self.region
            return f"region {r['x']},{r['y']} {r['width']}×{r['height']}"
        return "all monitors" if self.monitor == 0 else f"monitor {self.monitor}"


# -- platforms ---------------------------------------------------------------------

_PS_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type -Namespace Loom -Name Native -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
[DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
[StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
'@
[void][Loom.Native]::SetProcessDPIAware()
$monitor = __MONITOR__; $scale = __SCALE__; $fmt = '__FMT__'; $out = '__OUT__'
$rx = __RX__; $ry = __RY__; $rw = __RW__; $rh = __RH__; $win = '__WIN__'
if ($win -ne '') {
  $p = Get-Process | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -like ('*' + $win + '*') } | Select-Object -First 1
  if ($null -eq $p) { throw ('no window whose title contains "' + $win + '"') }
  $r = New-Object Loom.Native+RECT
  [void][Loom.Native]::GetWindowRect($p.MainWindowHandle, [ref]$r)
  $bounds = New-Object System.Drawing.Rectangle($r.Left, $r.Top, ($r.Right - $r.Left), ($r.Bottom - $r.Top))
} elseif ($rw -gt 0 -and $rh -gt 0) {
  $bounds = New-Object System.Drawing.Rectangle($rx, $ry, $rw, $rh)
} elseif ($monitor -eq 0) {
  $bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
} else {
  $ordered = @([System.Windows.Forms.Screen]::PrimaryScreen) + @([System.Windows.Forms.Screen]::AllScreens | Where-Object { -not $_.Primary })
  if ($monitor -gt $ordered.Count) { throw ('monitor ' + $monitor + ' does not exist; ' + $ordered.Count + ' attached') }
  $bounds = $ordered[$monitor - 1].Bounds
}
if ($bounds.Width -le 0 -or $bounds.Height -le 0) { throw 'empty capture area' }
$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$g.Dispose()
if ($scale -lt 1) {
  $w = [Math]::Max(1, [int]($bounds.Width * $scale)); $h = [Math]::Max(1, [int]($bounds.Height * $scale))
  $small = New-Object System.Drawing.Bitmap $w, $h
  $g2 = [System.Drawing.Graphics]::FromImage($small)
  $g2.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $g2.DrawImage($bmp, 0, 0, $w, $h); $g2.Dispose(); $bmp.Dispose(); $bmp = $small
}
if ($fmt -eq 'jpeg') { $bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Jpeg) } else { $bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png) }
$bmp.Dispose()
"""


def _ps_literal(text: str) -> str:
    return text.replace("'", "''")


def _windows_script(spec: CaptureSpec) -> str:
    r = spec.region or {}
    return (
        _PS_TEMPLATE.replace("__MONITOR__", str(int(spec.monitor)))
        .replace("__SCALE__", repr(float(spec.scale)))
        .replace("__FMT__", spec.fmt)
        .replace("__OUT__", _ps_literal(str(Path(spec.out_path))))
        .replace("__RX__", str(int(r.get("x", 0))))
        .replace("__RY__", str(int(r.get("y", 0))))
        .replace("__RW__", str(int(r.get("width", 0))))
        .replace("__RH__", str(int(r.get("height", 0))))
        .replace("__WIN__", _ps_literal(spec.window))
    )


async def _run(argv: list[str]) -> tuple[int, str]:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **kwargs
        )
    except (OSError, ValueError) as exc:
        raise CaptureError(f"cannot start {argv[0]}: {exc}") from exc
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=CAPTURE_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        raise CaptureError(f"{argv[0]} did not finish within {CAPTURE_TIMEOUT_S:g}s") from None
    return int(proc.returncode or 0), (out or b"").decode("utf-8", errors="replace").strip()


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:300]
    return ""


async def _capture_windows(spec: CaptureSpec) -> None:
    encoded = base64.b64encode(_windows_script(spec).encode("utf-16-le")).decode("ascii")
    code, out = await _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded])
    if code != 0:
        raise CaptureError(f"screen capture failed: {_first_line(out) or f'powershell exit {code}'}")


async def _capture_macos(spec: CaptureSpec) -> None:
    if spec.window:
        raise CaptureError("capturing by window title is not supported on macOS; use monitor or region")
    argv = ["screencapture", "-x", "-t", "jpg" if spec.fmt == "jpeg" else "png"]
    if spec.region:
        r = spec.region
        argv += ["-R", f"{r['x']},{r['y']},{r['width']},{r['height']}"]
    elif spec.monitor > 0:
        argv += ["-D", str(spec.monitor)]
    argv.append(spec.out_path)
    code, out = await _run(argv)
    if code != 0:
        raise CaptureError(f"screencapture failed: {_first_line(out) or f'exit {code}'}")


async def _capture_linux(spec: CaptureSpec) -> None:
    if spec.window:
        raise CaptureError("capturing by window title is not supported on Linux; use monitor or region")
    looked: list[str] = []
    for name in ("grim", "gnome-screenshot", "import", "scrot"):
        looked.append(name)
        if shutil.which(name) is None:
            continue
        if name == "grim":
            argv = ["grim"]
            if spec.region:
                r = spec.region
                argv += ["-g", f"{r['x']},{r['y']} {r['width']}x{r['height']}"]
            argv.append(spec.out_path)
        elif name == "gnome-screenshot":
            argv = ["gnome-screenshot", "-f", spec.out_path]
        elif name == "import":
            argv = ["import", "-window", "root", spec.out_path]
        else:
            argv = ["scrot", spec.out_path]
        code, out = await _run(argv)
        if code != 0:
            raise CaptureError(f"{name} failed: {_first_line(out) or f'exit {code}'}")
        return
    raise CaptureError(f"no screenshot tool found (looked for {', '.join(looked)})")


async def capture(spec: CaptureSpec) -> bytes:
    """Capture per `spec` into `spec.out_path`, return the bytes and remove the file.
    Raises `CaptureError` with a sentence the model can act on."""
    out = Path(spec.out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            await _capture_windows(spec)
        elif sys.platform == "darwin":
            await _capture_macos(spec)
        else:
            await _capture_linux(spec)
        try:
            return out.read_bytes()
        except OSError as exc:
            raise CaptureError(f"capture produced no file: {exc}") from exc
    finally:
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass


# -- the tool ----------------------------------------------------------------------

def _parse_region(raw: Any) -> dict[str, int] | None:
    if raw in (None, "", {}):
        return None
    if not isinstance(raw, dict):
        raise ValueError("region must be an object {x, y, width, height}")
    try:
        region = {k: int(raw.get(k, 0)) for k in ("x", "y", "width", "height")}
    except (TypeError, ValueError):
        raise ValueError("region values must be integers") from None
    if region["width"] <= 0 or region["height"] <= 0:
        raise ValueError("region width and height must be positive")
    return region


async def screenshot_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        monitor = int(args.get("monitor", 1) if args.get("monitor") is not None else 1)
        if monitor < 0:
            raise ValueError("monitor must be 0 (all) or 1 and up")
        region = _parse_region(args.get("region"))
        scale = float(args.get("scale") if args.get("scale") is not None else 1.0)
        if not 0.1 <= scale <= 1.0:
            raise ValueError("scale must be between 0.1 and 1.0")
    except (TypeError, ValueError) as exc:
        return ToolResult.error(str(exc))
    fmt = str(args.get("format") or "png").lower().replace("jpg", "jpeg")
    if fmt not in FORMATS:
        return ToolResult.error(f"format must be one of {', '.join(FORMATS)}")
    window = str(args.get("window") or "").strip()
    if scale < 1.0 and os.name != "nt":
        scale = 1.0  # only the Windows capture resizes; say so in the result below

    folder = Path(ctx.app.artifacts_dir) / ctx.conversation_id
    spec = CaptureSpec(
        out_path=str(folder / f"shot-{ulid()}.{'jpg' if fmt == 'jpeg' else 'png'}"),
        monitor=monitor, region=region, window=window, scale=scale, fmt=fmt,
    )
    try:
        data = await capture(spec)
    except CaptureError as exc:
        return ToolResult.error(str(exc))
    cap = int(ctx.app.settings.get("conversation.image_max_mb")) * 1024 * 1024
    if len(data) > cap:
        return ToolResult.error(
            f"screenshot is {len(data)} bytes, over the {cap // (1024 * 1024)} MB image cap (conversation.image_max_mb); lower scale or capture a region"
        )
    mime = sniff_image(data) or ("image/jpeg" if fmt == "jpeg" else "image/png")
    dims = image_dimensions(data)
    shape = f"{dims[0]}×{dims[1]} " if dims else ""
    note = "" if os.name == "nt" or float(args.get("scale") or 1.0) >= 1.0 else " (scale is not supported on this platform)"
    return ToolResult(
        content=f"{shape}{fmt}, {len(data)} bytes ({spec.describe()}){note}",
        images=[{"data": data, "mime": mime}],
        meta={"bytes": len(data), "mime": mime, "captured": spec.describe(), **({"width": dims[0], "height": dims[1]} if dims else {})},
    )


def tools() -> list[Tool]:
    return [
        Tool(
            name="screenshot",
            description=prompts.load("tools/screenshot"),
            parameters={
                "type": "object",
                "properties": {
                    "monitor": {"type": "integer", "description": "1 = primary monitor (default), 2… = the others, 0 = every monitor as one image.", "default": 1},
                    "region": {
                        "type": "object",
                        "description": "Capture only this rectangle, in screen pixels.",
                        "properties": {
                            "x": {"type": "integer"}, "y": {"type": "integer"},
                            "width": {"type": "integer"}, "height": {"type": "integer"},
                        },
                    },
                    "window": {"type": "string", "description": "Capture the top-level window whose title contains this text (Windows)."},
                    "scale": {"type": "number", "description": "Downscale factor 0.1–1.0; 0.5 halves width and height.", "default": 1.0},
                    "format": {"type": "string", "enum": list(FORMATS), "default": "png"},
                },
            },
            handler=screenshot_handler,
            read_only=True,
            timeout_s=30.0,
        )
    ]
