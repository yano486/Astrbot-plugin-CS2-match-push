# Third-party notices

## HLTV selector and transport reference

`hltv_parser.py` and `hltv_fetcher.py` adapt selector knowledge and transport
design from `Almighty-ap/astrbot_plugin_cs2_results`, specifically `hltv.py` and
`fetcher.py` at commit `7bfabd40700ae2bc84516050ee4dddb763b95866`:

https://github.com/Almighty-ap/astrbot_plugin_cs2_results/tree/7bfabd40700ae2bc84516050ee4dddb763b95866

The implementation here is deliberately smaller: a standard-library HTML
parser, PandaScore-compatible records, one low-frequency request gate, standard
`aiohttp` HTTP, optional ordinary Playwright browser loading, and an explicit
local human-verification window. It does
not copy the reference plugin's VNC/noVNC services, display/server setup,
fingerprint rotation, player statistics or application/store integration.
The offline fixtures are synthetic reduced DOM examples, not captured pages.
No HLTV content, trademarks, or website rights are licensed by the MIT notice.
The existing plugin's own license is not changed by this notice.

## Transport lifecycle note

`HltvClient.close()` is the owner shutdown boundary. It is idempotent and
cancels/awaits in-flight fetch and manual-verification tasks before closing
the standard HTTP session, shared Chromium context, and any injected transport
that exposes `close()`. It clears the in-memory validated-page cache and leaves
the status state as `closed`; a closed client never reopens transport. Caller
timeouts should cancel the individual call, while plugin teardown should await
`close()`. The parent scheduler should allow the configured request gap and
use a longer bound for explicit human verification; verification is never
started implicitly by a timeout. Browser and transport exceptions are replaced
by fixed credential-safe status messages, and no raw proxy URL or exception
text is returned by `status()` or `manual_verify()`.

### MIT License

Copyright (c) 2026 canxiaocai

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
