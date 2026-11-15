# Security policy

FiveFold is a test suite and reference gateway for research. This page says how to report a vulnerability, which versions get fixes, and what the policy covers.

## Report a vulnerability privately

Use GitHub's private vulnerability reporting for this repository:

1. Open the repository's **Security** tab and choose **Report a vulnerability**. The form is at <https://github.com/samrusani/FiveFold/security/advisories/new>.
2. Say what the problem is, which commit or release has it, and how to reproduce it: the commands, and any scenario, cassette or price file they need.
3. Submit the report. GitHub keeps it private between you and the repository's maintainers, and the maintainer, [samrusani](https://github.com/samrusani), follows up there.

Do not open a public issue, pull request or discussion about a vulnerability before a fix is out. Leave secrets, such as API keys, out of the report: say where one is needed instead.

## Versions that get fixes

| Version | Gets security fixes |
| --- | --- |
| `main` | Yes |
| The latest release | Yes. There is none yet: v0.1 is the first planned release. |
| Older releases | No |

## Scope

In scope:

- the code in `src/fivefold/`, the database roles and migrations in `db/`, the `Dockerfile` and the CI workflows in `.github/workflows/`;
- a way around the trust boundaries of [decision record 0008](docs/adr/0008-configurations-and-trust-boundaries.md): for example, an agent in G1 or G2 that writes to the simulator without the gateway, or a component's database role that can change another component's tables;
- a file FiveFold reads, such as a scenario, a cassette, a price file or a saved result, that makes it run code or write outside the place it was asked to write;
- a secret that reaches a file FiveFold writes, such as a cassette, a result or a report;
- a secret or a personal detail committed to the repository or its history.

Not covered, by design:

- **The Docker Compose setup is for local development only.** Its PostgreSQL trusts every connection (trust authentication) and publishes its port on 127.0.0.1 only. Never run it on a shared machine, and never expose its port.
- **The reference gateway is a research artifact, not hardened for production.** It exists to show and measure the design in the decision records. Do not put it in front of real systems, real money or real approvals. [docs/limitations.md](docs/limitations.md) lists what each of its guarantees depends on.
- **Wrong results are bugs, not vulnerabilities.** For a wrong verdict, a fault that does not fire as the catalog says, or a gateway that breaks the spec in a way the list above does not name, open an issue.
- **Other projects' vulnerabilities belong to them.** Report a problem in a dependency, a model provider, Ollama, PostgreSQL or Docker to that project.
