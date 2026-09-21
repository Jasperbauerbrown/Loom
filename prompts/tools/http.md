Make an HTTP request and return the response.

Arguments:
- `method` (string, required): GET, HEAD, POST, PUT, PATCH or DELETE.
- `url` (string, required): the absolute URL.
- `headers` (object, optional): request headers. Values may use `{{secret:NAME}}` — write `{"Authorization": "Bearer {{secret:GITHUB_TOKEN}}"}` rather than pasting a credential, which would then live in the transcript.
- `body` (string or object, optional): the request body. An object is sent as JSON with the matching content type.
- `timeout_s` (number, optional): seconds to wait; defaults to the configured HTTP timeout.
- `follow_redirects` (boolean, optional, default true).

Returns the status line, the headers worth seeing, and the body as text, with JSON pretty-printed. A response that is an image (png, jpeg, webp, gif, within the image size cap) comes back as the image itself. GET and HEAD run immediately; other methods change something on the other end and are judged first.
