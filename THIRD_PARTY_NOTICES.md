# Third-party notices

The repository is released under the [MIT License](LICENSE). These files come from other projects and keep their own licences.

| File | From | Licence |
|---|---|---|
| `software/tests/data/okay_nabu.wav` | [pymicro-wakeword](https://github.com/OHF-Voice/pymicro-wakeword), `tests/okay_nabu/2.wav`, by the Open Home Foundation and its contributors | [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0) |
| `software/tests/data/speech.wav` | [pysilero-vad](https://github.com/rhasspy/pysilero-vad), `tests/speech.wav` | MIT License, below |
| `tools/third_party/bluez/gatt-database-fix-freeing-wrong-client-notify-io.patch` | [BlueZ](https://www.bluez.org/) commit [`f7e2cd91`](https://git.kernel.org/pub/scm/bluetooth/bluez.git/commit/?id=f7e2cd91c2669cf50d07fe05caedf794e88f7476) by Jerry Wu, unchanged ([how](tools/third_party/bluez/README.md)) | [GNU General Public License v2.0 or later](https://spdx.org/licenses/GPL-2.0-or-later.html), BlueZ's licence for the file it changes |
| `ui/third_party/qrcodegen/qrcodegen.js` | [QR Code generator library](https://github.com/nayuki/QR-Code-generator) by Project Nayuki, built from its TypeScript ([how](ui/third_party/qrcodegen/README.md)) | MIT License, in the file's header |

Libraries installed as dependencies are not copied here: their licences come with them.

## pysilero-vad

```
MIT License

Copyright (c) 2025 Michael Hansen

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
