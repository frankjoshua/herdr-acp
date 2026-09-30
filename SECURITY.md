# Security

## Supported versions

Only the latest commit on `main` is supported.

## Reporting a vulnerability

Report it privately through GitHub's
[private vulnerability reporting](https://github.com/frankjoshua/herdr-acp/security/advisories/new),
not a public issue. Include what an attacker controls (the ACP client, the pane's contents, local
files) and the steps to reproduce. Expect a reply within a week.

## Scope

herdr-acp types into a Herdr pane on behalf of whatever ACP client spawned it, and answers the
pane's approval dialogs with that client's choice. Anyone who can drive its stdio can therefore
act as the person at the pane. It opens no network ports and stores no credentials.
