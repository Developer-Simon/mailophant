# AI disclaimer

## How this project was built

Large parts of this repository were written with the help of an AI coding
assistant: source code, tests, documentation and packaging. The assistant is
Claude (Anthropic), used through Claude Code.

[@Developer-Simon](https://github.com/Developer-Simon) is the human author and
maintainer. They review every change and are responsible for what ships. Every
line gets read, and a change counts as done only after the test suites
(`python3 -m unittest discover tests`, `npm test` in `electron/`) pass and it
works in the running app. If you find a bug, it is the maintainer's bug.

## Contributing: disclose your AI use

If you open a pull request, say in its description which AI tools, if any,
helped with the code. This is required.

## Why

Mailophant works on people's private mail archives, often the only copy of
years of correspondence. Knowing how a change was produced is part of being
able to trust it with that data.

The project is open source and meant to grow through contributions from other
people, who will use different tools. Disclosing AI use lets every
contribution be reviewed on the same terms.
