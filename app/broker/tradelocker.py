"""TradeLocker REST client.

This is a minimal but production-shaped implementation of the TradeLocker
backend-api authentication, account, quote and order endpoints. The exact
URI shapes used here mirror the public TradeLocker docs (``/auth/jwt/token``,
``/accounts``, ``/trade/...``); if the broker schema differs in your tenant,
override the ``_*_path`` constants below or subclass the client.

Design notes
------------
* All HTTP I/O uses ``httpx.AsyncClient``.
* JWT lifecycle is fully managed: token + refresh-token are cached, and a
  background refresh runs before expiry (decoded from the JWT ``exp`` claim).
* ``tenacity`` retries transient failures with exponential backoff.
* Credentials are pulled from ``Settings`` and never logged.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import jwt
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.broker.base import BrokerClient
from app.config import Settings
from app.models import Order, OrderStatus
from app.utils.logging import get_logger

log = get_logger(__name__)


class TradeLockerAuthError(RuntimeError):
    pass


class TradeLockerClient(BrokerClient):
    """Async TradeLocker REST client with JWT auth + auto-refresh."""

    # Endpoint paths - override in a subclass if your tenant differs.
    _auth_path = "/auth/jwt/token"
    _refresh_path = "/auth/jwt/refresh"
    _accounts_path = "/auth/jwt/all-accounts"
    _account_state_path = "/trade/accounts/{account_id}/state"
    _quote_path = "/trade/quotes"
    _orders_path = "/trade/accounts/{account_id}/orders"
    _positions_path = "/trade/accounts/{account_id}/positions"

    def __init__(self, settings: Settings, *, http: httpx.AsyncClient | None = None) -> None:
        self._s = settings
        self._http = http or httpx.AsyncClient(
            base_url=settings.tl_base_url,
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={"User-Agent": "prop-firm-scalp/0.1"},
        )
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._access_exp: float = 0.0
        self._auth_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None

    # ---- lifecycle -------------------------------------------------------
    async def connect(self) -> None:
        await self._authenticate()
        self._refresh_task = asyncio.create_task(self._refresh_loop(), name="tl-jwt-refresh")
        log.info("tradelocker connected", extra={"account_id": self._s.tl_account_id})

    async def close(self) -> None:
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._http.aclose()

    # ---- auth ------------------------------------------------------------
    async def _authenticate(self) -> None:
        async with self._auth_lock:
            payload = {
                "email": self._s.tl_email.get_secret_value(),
                "password": self._s.tl_password.get_secret_value(),
                "server": self._s.tl_server,
            }
            resp = await self._http.post(self._auth_path, json=payload)
            if resp.status_code >= 400:
                raise TradeLockerAuthError(
                    f"auth failed status={resp.status_code} body={resp.text[:200]}"
                )
            data = resp.json()
            self._access_token = data["accessToken"]
            self._refresh_token = data.get("refreshToken")
            self._access_exp = self._decode_exp(self._access_token)
            self._http.headers["Authorization"] = f"Bearer {self._access_token}"
            log.info("jwt acquired", extra={"exp_in_sec": int(self._access_exp - time.time())})

    async def _refresh(self) -> None:
        if not self._refresh_token:
            await self._authenticate()
            return
        async with self._auth_lock:
            resp = await self._http.post(
                self._refresh_path,
                json={"refreshToken": self._refresh_token},
            )
            if resp.status_code >= 400:
                log.warning("jwt refresh failed, re-authenticating")
                await self._authenticate()
                return
            data = resp.json()
            self._access_token = data["accessToken"]
            self._refresh_token = data.get("refreshToken", self._refresh_token)
            self._access_exp = self._decode_exp(self._access_token)
            self._http.headers["Authorization"] = f"Bearer {self._access_token}"
            log.info("jwt refreshed")

    async def _refresh_loop(self) -> None:
        """Background task: refresh ~60s before the access token expires."""
        try:
            while True:
                lead = max(self._access_exp - time.time() - 60, 5)
                await asyncio.sleep(lead)
                try:
                    await self._refresh()
                except Exception as exc:  # noqa: BLE001
                    log.exception("jwt refresh loop error: %s", exc)
                    await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _decode_exp(token: str) -> float:
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
            return float(claims.get("exp", time.time() + 600))
        except jwt.PyJWTError:
            # Fallback: assume 10-min lifetime
            return time.time() + 600

    # ---- generic request with retry --------------------------------------
    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, max=4),
            retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
            reraise=True,
        ):
            with attempt:
                resp = await self._http.request(method, path, **kwargs)
                if resp.status_code == 401:
                    log.warning("401 - re-authenticating")
                    await self._authenticate()
                    resp = await self._http.request(method, path, **kwargs)
                resp.raise_for_status()
                return resp
        raise RuntimeError("unreachable")

    # ---- public surface --------------------------------------------------
    async def get_account_balance(self) -> float:
        path = self._account_state_path.format(account_id=self._s.tl_account_id)
        resp = await self._request("GET", path, headers=self._account_headers())
        body = resp.json()
        # TradeLocker returns a key/value list under "accountDetailsData"; we
        # extract the balance field defensively.
        if isinstance(body, dict):
            for key in ("balance", "Balance", "accountBalance"):
                if key in body:
                    return float(body[key])
            data = body.get("d") or body.get("accountDetailsData") or {}
            if isinstance(data, dict) and "balance" in data:
                return float(data["balance"])
        log.warning("could not parse balance, defaulting to configured value")
        return self._s.account_balance

    async def get_quote(self, symbol: str) -> tuple[float, float]:
        resp = await self._request(
            "GET",
            self._quote_path,
            params={"symbol": symbol},
            headers=self._account_headers(),
        )
        body = resp.json()
        bid = float(body.get("bid") or body.get("b") or 0.0)
        ask = float(body.get("ask") or body.get("a") or 0.0)
        if bid <= 0 or ask <= 0:
            raise ValueError(f"invalid quote for {symbol}: {body}")
        return bid, ask

    async def place_order(self, order: Order) -> Order:
        path = self._orders_path.format(account_id=self._s.tl_account_id)
        payload = {
            "clientOrderId": order.client_order_id,
            "symbol": order.symbol,
            "side": order.side.value,
            "type": order.order_type.value,
            "quantity": order.quantity,
            "price": order.price,
            "stopLoss": order.stop_loss,
            "takeProfit": order.take_profit,
        }
        try:
            resp = await self._request("POST", path, json=payload, headers=self._account_headers())
        except httpx.HTTPStatusError as e:
            log.error("order rejected status=%s body=%s", e.response.status_code, e.response.text)
            return order.model_copy(update={
                "status": OrderStatus.REJECTED,
                "rejection_reason": e.response.text[:200],
            })
        body = resp.json()
        return order.model_copy(update={
            "status": OrderStatus.SUBMITTED,
            "broker_order_id": str(body.get("orderId") or body.get("id") or ""),
        })

    async def cancel_order(self, broker_order_id: str) -> bool:
        path = f"{self._orders_path.format(account_id=self._s.tl_account_id)}/{broker_order_id}"
        try:
            await self._request("DELETE", path, headers=self._account_headers())
            return True
        except httpx.HTTPStatusError as e:
            log.warning("cancel failed: %s", e)
            return False

    async def close_position(self, symbol: str) -> bool:
        path = self._positions_path.format(account_id=self._s.tl_account_id)
        try:
            await self._request(
                "DELETE",
                path,
                params={"symbol": symbol},
                headers=self._account_headers(),
            )
            return True
        except httpx.HTTPStatusError as e:
            log.warning("close_position failed: %s", e)
            return False

    # ---- helpers ---------------------------------------------------------
    def _account_headers(self) -> dict[str, str]:
        return {
            "accNum": self._s.tl_account_num,
            "accountId": self._s.tl_account_id,
        }
