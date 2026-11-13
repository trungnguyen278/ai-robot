# BlueZ patches

Upstream BlueZ commits that Ubuntu 26.04's `bluez` package lacks and the robot needs ([ADR-0020](../../../docs/decisions/0020-patched-bluez.md)). [`../../pi/patched-bluez.sh`](../../pi/patched-bluez.sh) adds each `.patch` here to Ubuntu's source package and builds it.

They are licensed as the BlueZ code they change: [GPL-2.0-or-later](https://spdx.org/licenses/GPL-2.0-or-later.html), as `src/gatt-database.c` states in its SPDX header.

| Patch | Upstream commit | Why |
|---|---|---|
| `gatt-database-fix-freeing-wrong-client-notify-io.patch` | [`f7e2cd91c2669cf50d07fe05caedf794e88f7476`](https://git.kernel.org/pub/scm/bluetooth/bluez.git/commit/?id=f7e2cd91c2669cf50d07fe05caedf794e88f7476), "gatt-database: Fix freeing wrong client notify IO", by Jerry Wu, committed 2026-10-26; first released in BlueZ 5.88 | When a device that is not bonded disconnects, BlueZ 5.85 closes the notification channel of the app that subscribed first instead of the one leaving. A second app turned away from set-up ended the first app's session. |

- **Source:** each file is the commit as `git.kernel.org` serves it (`/pub/scm/bluetooth/bluez.git/patch/?id=<commit>`), unchanged. SHA-256 of `gatt-database-fix-freeing-wrong-client-notify-io.patch`: `4c64d1ea02b994ff9b30c7a2a98d48512d937445a643e55637697acbde7d97ff`.
- **To add one:** fetch it the same way, add a row here and to [THIRD_PARTY_NOTICES.md](../../../THIRD_PARTY_NOTICES.md), and raise the build's version suffix in the script. Patches apply in the order of their names.
- **To drop one:** the script stops when Ubuntu's source already has a patch; then delete the file and its rows ([the steps](../../pi/README.md#when-ubuntus-bluez-has-the-fix)).
