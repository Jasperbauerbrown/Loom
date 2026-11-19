Take a screenshot and look at it. Use it to see what is on the person's screen, to check the result of something you started, or to read a window you cannot reach through a tool.

Arguments (all optional):
- `monitor` (integer, default 1): which monitor — 1 is the primary, 2 and up the others, 0 captures every monitor as one image.
- `region` (object `{x, y, width, height}`): only this rectangle, in screen pixels.
- `window` (string): the top-level window whose title contains this text, ignoring case (Windows). Captures the window's own pixels, so it still works when the window is behind another one or partly off-screen. A minimised window is refused: it has no pixels until it is restored.
- `scale` (number 0.1–1.0, default 1.0): downscale the capture; 0.5 halves width and height. Use it for a full-screen overview, and a `region` or `window` at full scale to read small text.
- `format` (`png` or `jpeg`, default `png`).

To look at a particular program, ask for it by `window` first, not by capturing the whole screen and searching it. When no title matches, the result lists the titles of every open window, so the same call tells you whether the program is open and what its window is really called. A browser window's title is its active tab's: a page in another tab, or not open at all, cannot be captured by window; open it yourself in a headless browser instead. Capture a `monitor` when the person's screen as a whole is what you need to see.

`region` and `monitor` capture whatever is visible on screen at that spot; something else on top comes through instead. Returns the image, with its size and what was captured in the text (for a `window`, the full title of the window it found). Nothing is brought to the foreground and nothing on screen changes. Oversized captures are refused; lower `scale` or narrow the `region`.
