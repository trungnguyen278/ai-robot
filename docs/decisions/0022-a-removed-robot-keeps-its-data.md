# 0022 — A robot removed in the app keeps its data: only its own reset erases it

- **Status:** Accepted. The owner approved the proposal on 2026-11-12; recorded on 2026-11-19.
- **Date:** 2026-11-12

## Context

The owner removes a robot in the app, or the home that holds it is deleted. The cloud then revokes its registration: it ends the robot's session with `bye` `revoked`, refuses its tokens with `invalid_client`, and forgets its key ([the app API](https://github.com/trungnguyen278/ai-platform/blob/main/protocols/app/README.md#removing-a-device), "Removing a device"). The robot learns it either way and offers set-up again (PROV-3).

PROV-4 says a robot passes to a new owner only through a reset that erases the previous owner's data and binding ([requirements](https://github.com/trungnguyen278/ai-platform/blob/main/protocols/cloud/requirements.md)). The protocols already say that removing a device does not erase it. What the robot itself keeps once revoked was left open: its Wi-Fi networks, its name, its settings, its identity, and the server it knew.

What large products do (read 2026-11-13):
- Google: "Removing the device removes it from your existing home", and "Factory reset your device before you add it to another home" ([support.google.com/googlehome/answer/13127223](https://support.google.com/googlehome/answer/13127223)).
- Apple: "After you remove the accessories, follow the manufacturer's instructions to reset them" ([support.apple.com/102056](https://support.apple.com/102056)).
- Amazon: deregistering removes a device from the account, and its help pages recommend both deregistering and resetting, as two steps.

## Decision

**A revocation only unlinks the robot.** It keeps its Wi-Fi networks, its name, its settings, its identity and the server it knew, and offers set-up again. Its binding says that it is not bound. **The only thing that erases it is the reset on its own screen** ([ADR-0023](0023-factory-reset.md)). The cloud has no command that erases a robot.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Erase on revocation, as removing every account from a Meta Portal resets it | A revocation reaches the robot from the network. Whoever holds the owner's account could then wipe the robot, and a mistaken removal would cost the owner the Wi-Fi password and every setting. An erase also needs a person beside the robot to set it up again, so it gains nothing remotely. |
| Erase part of it, such as the settings, and keep the Wi-Fi | Still an erase from the network, and harder to explain than either rule. |
| Refuse set-up after a revocation until a reset | The app API lets a removed device be set up again, as a new device, in any home. Setting it up needs the code on its screen, so a person beside it. |

## Consequences

- Positive:
  - Removing by mistake costs nothing: the owner sets the robot up again, and its Wi-Fi and settings are still there.
  - Nothing from the network erases the robot.
- Negative / accepted trade-offs:
  - Until someone resets it, a removed robot holds its previous owner's Wi-Fi password, name and settings. Whoever holds it can set it up into another home with them.
  - The app's removal dialog says how to reset the robot before giving it away: Settings, About, Reset to factory settings ([ADR-0023](0023-factory-reset.md)).
- Follow-up: ai-app names the reset's path in its removal dialog.

## References

- `software/tests/test_factory_reset.py`, `test_a_revoked_robot_erases_nothing`: a revocation keeps the settings, the key file and the server, and starts no reset.
- [ai-platform ADR-0012](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0012-registering-devices.md) (registering devices) and [ADR-0015](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0015-factory-reset.md) (the factory reset, across the repositories).
