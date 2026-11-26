# The Philosophy of Sprint Relay

[日本語](PHILOSOPHY.md) | **English**

This document describes the origins of Sprint Relay, the values underlying its design, and a critical examination of its current concept. For precise functional requirements, refer to the [current specification](sprint-relay-v2-specification.md). The future directions described here do not imply that the corresponding features have been implemented or their effectiveness verified.

## 1. Origins

Sprint Relay originated from a question: how can developers who actively use generative AI and those who value designing and implementing software themselves coexist within a small development team?

Developers differ in their technical interests, development practices, and approaches to using AI. Nevertheless, sharing specifications, coordinating responsibilities, managing dependencies, and tracking progress remain necessary regardless of individual development practices. Small teams may not have a dedicated person responsible for these management activities.

Sprint Relay was conceived as a way to delegate some of these responsibilities to AI, reduce misunderstandings and conflicts, and allow people to spend more time on the work they wish to pursue. Its priorities are reducing administrative effort, preventing misunderstandings and work conflicts, and making progress visible. AI-driven management is not an end in itself.

## 2. Human Agency

Technological and social activities are founded on human will and responsibility. AI is a means of supporting these activities, not an agent entitled to determine their purposes or values in place of humans.

This position does not reject the use of AI itself. Whether to delegate implementation to AI or undertake it independently should be a human decision based on the intended purpose. However, delegating work must be distinguished from relinquishing judgment and responsibility.

Sprint Relay emphasizes the following principles:

- Humans retain decision-making authority over project objectives, significant specifications, scope, responsibilities, and deadlines.
- Humans must be able to examine, challenge, and, when necessary, reject AI-generated proposals and audit findings.
- Developers' discretion over their use of AI and implementation practices must be respected. The authority of management AI must remain distinct from that of AI used independently by developers.
- AI outputs must not be trusted unconditionally. The basis for decisions and the allocation of responsibility must remain clear.
- Progress counts and working hours must not be treated as measures of individual developers' abilities or performance.
- Humans must be able to continue development even when AI is unavailable.

Some of these principles are embodied in the current specification through approval procedures, permission boundaries, auditing, and manual operation. However, the mere existence of formal approval authority does not necessarily preserve human agency.

## 3. The Contradictions Inherent in Automated Management

Sprint Relay is designed with human control over AI in mind. Viewed from another perspective, however, its structure can also be understood as a system through which AI governs human work.

AI decomposes specifications into tasks, adjusts priorities, monitors progress, and identifies problems. Even if humans retain final decision-making authority, their daily activities may become dictated by the system, reducing their role to merely endorsing decisions made by AI.

A development environment that treats developers as machinery expected to produce predetermined outputs within predetermined periods is undesirable. Quantifiable progress and efficiency represent only one aspect of development. Experimentation, learning, creativity, and independent judgment possess value that cannot be captured through simple quantification.

Furthermore, strict procedures established to protect human decision-making authority may themselves restrict human freedom and discretion. Attempts to reduce administrative burdens through automation may instead create new forms of administrative burden.

This contradiction is not external to Sprint Relay. It is inherent in Sprint Relay itself. The principle of human agency is not a declaration that the current implementation has successfully realized that principle.

## 4. Applicability and Limitations

Sprint Relay is not considered to possess universal or absolute value.

The extent to which management is necessary depends on team size, mutual trust, development duration, task complexity, and established development culture. Teams that can operate effectively using existing tools and direct human communication have no reason to adopt Sprint Relay.

Rather than imposing a uniform level of managerial rigor, users should be able to choose the intensity of management and the extent of AI intervention according to their circumstances. Future directions include relaxing rules that unnecessarily constrain developers, introducing profiles for different contexts, and improving the user interface and experience. However, relaxing managerial constraints is not equivalent to abandoning safeguards concerning authentication, authorization, and consent to data sharing.

Sprint Relay is not intended to impose a uniform development methodology on every team or individual.

## 5. Contributions

Opinions and contributions from those interested in the problems addressed by Sprint Relay are welcome.

Agreement with the current implementation or proposed approach is not required. Criticism of the design philosophy, identification of practical problems, UI/UX proposals, and simpler alternative approaches are as important as changes to the source code.

Sprint Relay itself, including its commitment to respecting human agency, must remain open to examination and criticism.

## Related Documents

- [README](README.md)
- [Current specification](sprint-relay-v2-specification.md)
- [Original specification and development history](sprint-relay-implementation.md)
- [Meeting, specification, and task publication workflow](docs/workflow.md)
- [Management agent instructions](docs/agents/management.md)
