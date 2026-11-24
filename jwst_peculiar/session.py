"""An HTTP session for the catalogue services, kept apart so importing it is cheap."""

from __future__ import annotations

from typing import Any

import requests
from typing_extensions import override

# seconds a service may go quiet before the next mirror is tried
TIMEOUT_S = 60


class PatientSession(requests.Session):
    """A session that gives up on a stalled server, which pyvo's own never does."""

    @override
    def request(
        self, method: str, url: Any, *args: Any, **kwargs: Any
    ) -> requests.Response:
        # read when called, so a test can shorten it
        kwargs.setdefault("timeout", TIMEOUT_S)
        return super().request(method, url, *args, **kwargs)
