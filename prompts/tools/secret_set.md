Store a credential in the operating system's keychain under a name.

Arguments:
- `name` (string, required): the name to store it under, e.g. `GITHUB_TOKEN`. Letters, digits, `_`, `.` and `-`.
- `value` (string, required): the secret itself, exactly as the user gave it.

Returns the name and the length — never the value. Once stored, use it as `{{secret:NAME}}` in tool arguments; it is substituted as the call executes and never enters the transcript. Use this the moment a user pastes a credential, then refer to it by name.
