# Contributing to Sprint Relay

[日本語](CONTRIBUTING.md) | **English**

Contributions to Sprint Relay are welcome.

For the project's background, design philosophy, and underlying concerns, please refer to [PHILOSOPHY.en.md](PHILOSOPHY.en.md).

## 1. Types of Contributions

Contributions are not limited to changes to the source code.

We welcome:

- Bug reports and fixes
- New features and improvements
- UI/UX improvements
- Architectural and design improvements
- New and improved tests
- Documentation improvements
- Security improvements
- Discussion and criticism of the design philosophy and underlying concepts
- Feedback based on actual use

Agreement with the current design or implementation is not a prerequisite for contributing.

## 2. Issues

Use GitHub Issues to report bugs, propose features, or raise design-related concerns.

Before opening an issue, check whether a similar issue already exists.

### Bug Reports

Please provide the following information where applicable:

- Description of the problem
- Steps to reproduce
- Expected behavior
- Actual behavior
- Execution environment
- Relevant logs or error messages

### Feature Proposals and Design Discussions

We recommend including:

- Problems with the current approach
- Proposed changes
- Purpose and rationale
- Expected impact
- Alternative approaches, if any

For substantial feature additions, specification changes, or architectural changes, discussion in an issue before implementation is recommended.

Minor corrections and small bug fixes do not require a prior issue.

## 3. Development Environment

Refer to [README.md](README.md) and the relevant documentation under `docs/` for instructions on setting up and running the development environment.

Review the current specification and existing implementation before making changes.

- [Current specification](sprint-relay-v2-specification.md)
- [Development guidelines](AGENTS.md)

## 4. Pull Requests

To propose changes, fork the repository, create a working branch, and submit a pull request.

Pull requests should include:

- A summary of the changes
- The rationale for the changes
- Related issues, if any
- Tests performed and their results
- Impact on existing functionality and specifications
- Remaining issues or limitations, if any

Keep each change focused on a single purpose whenever possible.

Avoid extensive refactoring or formatting changes unrelated to the purpose of the pull request.

Update the relevant documentation when making changes to the specification.

## 5. Testing and Quality

Run the relevant tests when modifying source code.

```sh
python -m pytest -q
```

Add tests where appropriate when introducing features or fixing bugs.

Consider compatibility with existing functionality, maintainability, security, and the impact on permission boundaries.

If certain aspects could not be verified due to environmental limitations, such as access to external services, state these limitations in the pull request.

Do not claim to have performed tests that were not actually run.

## 6. Use of Generative AI

The project does not impose restrictions on the use of generative AI as a means of contributing.

Whether and to what extent generative AI is used is left to the discretion of each contributor.

Regardless of whether generative AI was used, contributors are responsible for reviewing and taking responsibility for the changes they submit.

## 7. Review and Acceptance

Pull requests are evaluated primarily on the following criteria:

- Necessity of the changes
- Alignment with the project's objectives and design philosophy
- Technical soundness
- Maintainability
- Security
- Impact on existing functionality

Additional explanations or revisions may be requested during the review process.

Not every proposal or pull request will be accepted. Final decisions are made by the maintainers.

## 8. Security

Do not include credentials, API keys, access tokens, private meeting records, or personal information in issues, pull requests, or commits.

If you discover a security vulnerability, follow the [security policy](SECURITY.en.md) and report it privately to the maintainers before publicly disclosing exploitable details.

## 9. Languages

Japanese and English may be used in issues, pull requests, and discussions.

## 10. Communication

Technical disagreements and criticism of design decisions are welcome.

Discussions should focus on proposals, designs, implementations, and their underlying reasoning rather than on individual participants.

Respect differences in technical interests, development practices, and values.
