Take a screenshot and look at it. Use it to see what is on the person's screen, to check the result of something you started, or to read a window you cannot reach through a tool.

Arguments (all optional):
- `monitor` (integer, default 1): which monitor — 1 is the primary, 2 and up the others, 0 captures every monitor as one image.
- `region` (object `{x, y, width, height}`): only this rectangle, in screen pixels.
- `window` (string): the top-level window whose title contains this text (Windows).
- `scale` (number 0.1–1.0, default 1.0): downscale the capture; 0.5 halves width and height. Use it for a full-screen overview, and a `region` or `window` at full scale to read small text.
- `format` (`png` or `jpeg`, default `png`).

Returns the image, with its size and what was captured in the text. Nothing is brought to the foreground and nothing on screen changes. Oversized captures are refused; lower `scale` or narrow the `region`.
