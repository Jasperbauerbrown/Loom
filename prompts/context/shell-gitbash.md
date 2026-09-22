Git Bash (MSYS2) rewrites a leading single slash as a path, so Windows switches must be
doubled: `taskkill //F //T //PID 1234`, `tasklist //FI "PID eq 1234"`. A single slash fails
with errors like `Invalid argument/option - 'F:/'`.
Redirect to `/dev/null`, never `nul`: `2>nul` creates a file called `nul` in the working
directory.
`timeout` is the POSIX one, so `timeout /t 10` fails; use `sleep`. `wmic` is absent — for a
process's command line or its parent, call PowerShell: `powershell -NoProfile -Command
"Get-CimInstance Win32_Process -Filter \"ProcessId=1234\" | Select-Object
ProcessId,ParentProcessId,CommandLine"`.
