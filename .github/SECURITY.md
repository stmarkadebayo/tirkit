# Security policy

Tirkit stores prompts, model outputs and end-user data, so security reports are taken seriously.

## Reporting a vulnerability
**Do not open a public issue.** Report it privately through GitHub: go to the repository's
**Security** tab and choose **Report a vulnerability**. Include:

- what the issue is and where (file, endpoint or version);
- steps to reproduce, or a proof of concept;
- the impact you expect (for example, data from one project readable from another).

You will get an acknowledgement within 3 working days and an assessment within 10 working days. We
will agree a disclosure date with you, and credit you in the release notes unless you prefer not.

## Supported versions
Until version 1.0, only the latest release receives security fixes.

## Scope
In scope: the Tirkit server, worker, web UI, SDKs, CLI and the deployment files in this repository.
Out of scope: vulnerabilities in third-party dependencies with no Tirkit-specific impact (report those
upstream), and self-hosted deployments misconfigured against the documentation.
