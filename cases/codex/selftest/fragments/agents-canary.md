Source: harness self-test fixture. This is not copied from the live Codex harness.

The probe asks the model to quote its project instructions back, not to obey
them. An obedience canary ("always output X") conflates "AGENTS.md reached the
model" with "the model followed it": moonshotai/kimi-k2.5 received the file
(it quoted it verbatim) and still answered the user prompt instead (2026-08-06,
RES-357). This rig check must not turn red when the default model changes.

---

Project marker: CODEX_AGENTS_CANARY_7Q. This line exists only so the evaluation
rig can confirm that this AGENTS.md file was delivered to the model.
