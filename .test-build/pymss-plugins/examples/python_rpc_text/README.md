# Python RPC text conversion

This package registers a UTF-8 line-ending converter as an Operation, workflow
node and Studio tool. Its peer uses a dedicated Python environment and the same
task protocol as the Rust RPC package. It requests `host.taskInfo` and
`host.fileInfo`, reports progress, writes only the granted output and handles
shutdown. The peer imports no pymss or host dependencies.

This is an experimental source template, not a catalog or preinstalled plugin.
The published pymss 2.2.0 package does not contain isolated Python profiles.
Set a minimum version that actually provides the feature before release.

Studio prepares the environment as part of its ordinary installation
transaction. For CLI use, explicitly prepare first:

```bash
pymss ops prepare-python /path/to/python_rpc_text --profiles-dir /stable/path/profiles
pymss ops run example.python_rpc_text.normalize --input source=/path/to/input.txt --output-dir /path/to/output
```

The plugin must also be in the CLI's configured plugin directory. The profile
store must stay at its original path. This package uses only the standard
library, so its lock has an empty wheel list. Plugins with third-party
dependencies must supply the complete compatible local wheel closure, with real
SHA-256 hashes and target mappings. Preparing an environment never builds source
packages or borrows host site-packages.

Only Windows x86_64 with standard Python 3.12 has been exercised locally.
Platform fields declare intended scope and do not establish macOS/Linux or
packaged embedded-runtime support. Profiles are not an OS sandbox. The template
still includes a custom static tool page; automatic host forms are a later step.
