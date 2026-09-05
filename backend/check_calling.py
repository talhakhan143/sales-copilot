"""Check that calling is set up, and say exactly what is missing.

You put your keys in backend/.env yourself. This never asks you to type a secret
anywhere else, and it never prints one back.

    backend/.venv/bin/python check_calling.py

It talks to the real Twilio and Meta APIs, so a green line here means the thing
actually works, not that the value merely looks right.
"""
from __future__ import annotations

import asyncio
import base64
import sys

import httpx

sys.path.insert(0, ".")

from app.config import public_wss_base, settings  # noqa: E402

OK = "\033[92m ok \033[0m"
NO = "\033[91mfail\033[0m"
WARN = "\033[93mtodo\033[0m"
DIM = "\033[90m"
END = "\033[0m"

problems: list[str] = []


def line(state: str, label: str, detail: str = "") -> None:
    print(f"  [{state}] {label}" + (f"\n         {DIM}{detail}{END}" if detail else ""))


def todo(msg: str) -> None:
    problems.append(msg)


def mask(value: str) -> str:
    """Show only enough of a secret to tell two apart."""
    v = value.strip()
    if len(v) <= 8:
        return "set" if v else "empty"
    return f"{v[:4]}...{v[-4:]}"


async def check_tunnel() -> None:
    print("\nPUBLIC ADDRESS  (Twilio has to reach this machine from the internet)")
    base = settings.public_base_url.strip()
    if not base:
        line(WARN, "PUBLIC_BASE_URL is empty")
        todo("Start the tunnel with:  cloudflared tunnel --url http://localhost:8000\n"
             "    then put the https address it prints into PUBLIC_BASE_URL in backend/.env")
        return
    if "localhost" in base or "127.0.0.1" in base:
        line(NO, "PUBLIC_BASE_URL points at this machine only", base)
        todo("Twilio cannot reach localhost. Use the cloudflared address instead.")
        return
    try:
        async with httpx.AsyncClient(timeout=25.0) as c:
            r = await c.get(f"{base.rstrip('/')}/api/health")
        if r.status_code == 200 and r.json().get("status") == "ok":
            line(OK, "the internet can reach your backend", base)
            line(OK, "Twilio will send call audio to", f"{public_wss_base()}/ws/twilio")
        else:
            line(NO, "the address answered, but not with your backend", f"HTTP {r.status_code}")
            todo("Check the tunnel is still running and pointing at port 8000.")
    except Exception as e:  # noqa: BLE001
        line(NO, "could not reach that address", f"{type(e).__name__}: {e}")
        todo("The tunnel may have stopped. Start it again and put the new address in .env.\n"
             "    A free cloudflared address changes every time you restart it.")


async def check_twilio() -> None:
    print("\nTWILIO  (phone call from the app, costs money)")
    sid = settings.twilio_account_sid.strip()
    tok = settings.twilio_auth_token.strip()
    frm = settings.twilio_from_number.strip()

    if not sid or not tok:
        line(WARN, "no Twilio keys yet")
        todo("Open https://console.twilio.com and copy Account SID and Auth Token from the\n"
             "    front page, then put them in backend/.env as TWILIO_ACCOUNT_SID and\n"
             "    TWILIO_AUTH_TOKEN.")
        return
    if not sid.startswith("AC") or len(sid) != 34:
        line(NO, "TWILIO_ACCOUNT_SID does not look like a Twilio sid", mask(sid))
        todo("An Account SID starts with AC and is 34 characters. You may have copied the\n"
             "    wrong field. It is on the front page of console.twilio.com.")
        return

    auth = base64.b64encode(f"{sid}:{tok}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}
    async with httpx.AsyncClient(timeout=25.0) as c:
        r = await c.get(f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json", headers=headers)
        if r.status_code == 401:
            line(NO, "Twilio refused those keys")
            todo("The Auth Token is wrong. On console.twilio.com press the eye icon to reveal\n"
                 "    it, copy the whole thing, and paste it into TWILIO_AUTH_TOKEN.")
            return
        if r.status_code != 200:
            line(NO, "Twilio answered with an error", f"HTTP {r.status_code} {r.text[:120]}")
            todo("Check the Account SID and Auth Token are from the same account.")
            return

        acct = r.json()
        kind = acct.get("type", "?")
        line(OK, "keys work", f"account '{acct.get('friendly_name')}', {kind}")
        if kind == "Trial":
            line(WARN, "this is a trial account")
            todo("A trial account can only call numbers you have verified. Add the client\n"
                 "    number at https://console.twilio.com/us1/develop/phone-numbers/manage/verified\n"
                 "    or upgrade the account.")

        r = await c.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers.json?PageSize=20",
            headers=headers)
        nums = r.json().get("incoming_phone_numbers", []) if r.status_code == 200 else []
        if not nums:
            line(NO, "you do not own a Twilio number yet")
            todo("Buy one at https://console.twilio.com/us1/develop/phone-numbers/manage/search\n"
                 "    Pick one with Voice capability. It costs about one dollar a month.")
            return

        line(OK, f"you own {len(nums)} number(s)")
        for n in nums:
            voice = n.get("capabilities", {}).get("voice")
            print(f"         {DIM}{n.get('phone_number')}  voice={'yes' if voice else 'NO'}{END}")

        owned = {n.get("phone_number") for n in nums}
        if not frm:
            line(WARN, "TWILIO_FROM_NUMBER is empty")
            todo(f"Put one of the numbers above into TWILIO_FROM_NUMBER, for example\n"
                 f"    TWILIO_FROM_NUMBER={sorted(owned)[0]}")
        elif frm not in owned:
            line(NO, "TWILIO_FROM_NUMBER is not one of your numbers", frm)
            todo(f"Change it to one you own, for example {sorted(owned)[0]}")
        else:
            match = next(n for n in nums if n.get("phone_number") == frm)
            if not match.get("capabilities", {}).get("voice"):
                line(NO, "that number cannot make voice calls", frm)
                todo("Pick a number with Voice capability instead.")
            else:
                line(OK, "TWILIO_FROM_NUMBER is yours and can call", frm)


async def check_whatsapp() -> None:
    print("\nWHATSAPP")
    line(OK, "opening WhatsApp on your phone works with no setup at all",
         "pick 'WhatsApp, from my phone' in the app")

    tok = settings.whatsapp_token.strip()
    pid = settings.whatsapp_phone_id.strip()
    if not tok or not pid:
        line(WARN, "calling from inside the app is not set up")
        todo("This one needs a Meta Business account and a WhatsApp Business number with\n"
             "    calling switched on, which Meta has to approve. It is real work and it is\n"
             "    not needed for the free option above. Skip it unless you want the app to\n"
             "    dial WhatsApp by itself.")
        return

    ver = settings.whatsapp_api_version.strip() or "v21.0"
    async with httpx.AsyncClient(timeout=25.0) as c:
        r = await c.get(f"https://graph.facebook.com/{ver}/{pid}",
                        params={"fields": "display_phone_number,verified_name"},
                        headers={"Authorization": f"Bearer {tok}"})
    if r.status_code == 200:
        d = r.json()
        line(OK, "Meta accepted the token",
             f"{d.get('verified_name')} on {d.get('display_phone_number')}")
    else:
        err = r.json().get("error", {}) if r.headers.get("content-type", "").startswith("application/json") else {}
        line(NO, "Meta refused", f"{err.get('message', r.text[:120])}")
        todo("Check WHATSAPP_TOKEN has not expired and WHATSAPP_PHONE_ID is the phone number\n"
             "    id from the WhatsApp section of your Meta app, not the phone number itself.")


async def main() -> int:
    print("=" * 66)
    print("  Calling setup check")
    print("=" * 66)
    await check_tunnel()
    await check_twilio()
    await check_whatsapp()

    print("\n" + "=" * 66)
    if not problems:
        print("  Everything is ready. Open the app, press CALL, and pick how to dial.")
        return 0
    print(f"  {len(problems)} thing(s) left to do:\n")
    for i, p in enumerate(problems, 1):
        print(f"  {i}. {p}\n")
    print("  Fix them in backend/.env, then run this again.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
