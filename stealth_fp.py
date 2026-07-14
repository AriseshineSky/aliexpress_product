"""Stealth, fingerprint pool, and human-like mouse helpers for AliExpress crawl."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
FINGERPRINT_STORE = Path(
    os.environ.get("FINGERPRINT_STORE", str(BASE_DIR / "data" / "fingerprints.json"))
)

STEALTH_ENABLED = os.environ.get("STEALTH_ENABLED", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
FINGERPRINT_ENABLED = os.environ.get("FINGERPRINT_ENABLED", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
HUMAN_MOUSE_ENABLED = os.environ.get("HUMAN_MOUSE_ENABLED", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# Stable Chrome UA / GPU pairs that look consistent on Windows.
_UA_TEMPLATES = (
    # Windows 10
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36"
    ),
    # Windows 11 (still reports as NT 10.0 in Chrome UA, keep a second template slot
    # with identical shape but distinct seed path for variety weighting)
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36"
    ),
)

_GPU_PROFILES = (
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Ti Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 580 Series Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 7600 Direct3D11 vs_5_0 ps_5_0)"),
)

_VIEWPORTS = (
    (1920, 1080),
    (1680, 1050),
    (1600, 900),
    (1536, 864),
    (1440, 900),
    (1400, 1050),
    (1366, 768),
    (1280, 800),
    (1280, 720),
    (2560, 1440),
    (1920, 1200),
    (1680, 945),
)

_CHROME_MAJORS = (120, 121, 122, 123, 124, 125, 126, 127, 128, 129, 130, 131)
_HW_CORES = (4, 6, 8, 8, 12, 12, 16)
_DEVICE_MEM = (4, 8, 8, 8, 16, 16)


@dataclass(frozen=True)
class FingerprintProfile:
    key: str
    seed: int
    user_agent: str
    platform: str
    languages: tuple[str, ...]
    viewport_width: int
    viewport_height: int
    hardware_concurrency: int
    device_memory: int
    webgl_vendor: str
    webgl_renderer: str
    timezone_id: str
    locale: str

    def label(self) -> str:
        gpu = self.webgl_renderer.split(",")[0][:40]
        return (
            f"{self.key} ua=Chrome/{self._chrome_ver()} "
            f"{self.viewport_width}x{self.viewport_height} gpu={gpu}"
        )

    def _chrome_ver(self) -> str:
        for part in self.user_agent.split():
            if part.startswith("Chrome/"):
                return part.split("/")[1].split(".")[0]
        return "?"


def _stable_int(text: str, mod: int) -> int:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return int(digest[:12], 16) % mod


def build_fingerprint(key: str, *, timezone_id: str = "America/New_York") -> FingerprintProfile:
    """Deterministic fingerprint from a stable key (e.g. proxy host:port or worker id)."""
    chrome_ver = _CHROME_MAJORS[_stable_int(key + ":chrome", len(_CHROME_MAJORS))]
    vw, vh = _VIEWPORTS[_stable_int(key + ":vp", len(_VIEWPORTS))]
    vendor, renderer = _GPU_PROFILES[_stable_int(key + ":gpu", len(_GPU_PROFILES))]
    cores = _HW_CORES[_stable_int(key + ":cores", len(_HW_CORES))]
    mem = _DEVICE_MEM[_stable_int(key + ":mem", len(_DEVICE_MEM))]
    ua_tmpl = _UA_TEMPLATES[_stable_int(key + ":ua", len(_UA_TEMPLATES))]
    seed = _stable_int(key + ":seed", 2**31 - 1) or 1
    return FingerprintProfile(
        key=key,
        seed=seed,
        user_agent=ua_tmpl.format(ver=chrome_ver),
        platform="Win32",
        languages=("en-US", "en"),
        viewport_width=vw,
        viewport_height=vh,
        hardware_concurrency=cores,
        device_memory=mem,
        webgl_vendor=vendor,
        webgl_renderer=renderer,
        timezone_id=timezone_id,
        locale="en-US",
    )


def fingerprint_signature(fp: FingerprintProfile) -> tuple[str, int, int, str, int]:
    return (
        fp.user_agent,
        fp.viewport_width,
        fp.viewport_height,
        fp.webgl_renderer,
        fp.hardware_concurrency,
    )


def ensure_diverse_fingerprints(
    keys: list[str],
    *,
    timezone_id: str = "America/New_York",
    force_regenerate: bool = False,
) -> list[FingerprintProfile]:
    """Create/refresh fingerprints so each key gets a visibly different profile."""
    results: list[FingerprintProfile] = []
    used: set[tuple[str, int, int, str, int]] = set()
    for key in keys:
        if force_regenerate:
            fp = regenerate_fingerprint(key, timezone_id=timezone_id)
        else:
            fp = load_or_create_fingerprint(key, timezone_id=timezone_id)
        sig = fingerprint_signature(fp)
        if sig in used:
            for _ in range(48):
                fp = regenerate_fingerprint(key, timezone_id=timezone_id, avoid=fp)
                sig = fingerprint_signature(fp)
                if sig not in used:
                    break
        used.add(sig)
        results.append(fp)
    return results


def fingerprint_key_for_worker(worker_id: int, proxy_label: str | None = None) -> str:
    if proxy_label:
        return f"proxy:{proxy_label}"
    return f"worker:{worker_id}"


def fingerprint_to_dict(fp: FingerprintProfile) -> dict[str, Any]:
    data = asdict(fp)
    data["languages"] = list(fp.languages)
    return data


def fingerprint_from_dict(
    raw: dict[str, Any],
    *,
    key: str | None = None,
    timezone_id: str = "America/New_York",
) -> FingerprintProfile:
    """Parse a fingerprint dict (e.g. from Redis JSON). Optionally remount under ``key``."""
    bind_key = key if key is not None else str(raw.get("key") or "anonymous")
    return _fingerprint_from_raw(bind_key, raw, timezone_id=timezone_id)


def mint_random_fingerprint(
    *,
    key: str | None = None,
    timezone_id: str = "America/New_York",
) -> FingerprintProfile:
    """Create a fresh random profile (for Redis fingerprint producers; not persisted)."""
    fp_key = key or f"gen:{random.randrange(1, 1 << 62):x}"
    candidate = build_fingerprint(
        f"{fp_key}:mint:{random.randrange(1, 1 << 62)}",
        timezone_id=timezone_id,
    )
    return FingerprintProfile(
        key=fp_key,
        seed=candidate.seed,
        user_agent=candidate.user_agent,
        platform=candidate.platform,
        languages=candidate.languages,
        viewport_width=candidate.viewport_width,
        viewport_height=candidate.viewport_height,
        hardware_concurrency=candidate.hardware_concurrency,
        device_memory=candidate.device_memory,
        webgl_vendor=candidate.webgl_vendor,
        webgl_renderer=candidate.webgl_renderer,
        timezone_id=candidate.timezone_id,
        locale=candidate.locale,
    )


def rebind_and_save_fingerprint(
    fp: FingerprintProfile,
    key: str,
) -> FingerprintProfile:
    """Store ``fp`` under a new key (e.g. bind Redis FP to current proxy)."""
    bound = FingerprintProfile(
        key=key,
        seed=fp.seed,
        user_agent=fp.user_agent,
        platform=fp.platform,
        languages=fp.languages,
        viewport_width=fp.viewport_width,
        viewport_height=fp.viewport_height,
        hardware_concurrency=fp.hardware_concurrency,
        device_memory=fp.device_memory,
        webgl_vendor=fp.webgl_vendor,
        webgl_renderer=fp.webgl_renderer,
        timezone_id=fp.timezone_id,
        locale=fp.locale,
    )
    return _save_fingerprint(bound)


def _fingerprint_from_raw(
    key: str, raw: dict[str, Any], *, timezone_id: str = "America/New_York"
) -> FingerprintProfile:
    langs = raw.get("languages") or ["en-US", "en"]
    return FingerprintProfile(
        key=key,
        seed=int(raw.get("seed") or 1),
        user_agent=str(raw["user_agent"]),
        platform=str(raw.get("platform") or "Win32"),
        languages=tuple(langs),
        viewport_width=int(raw.get("viewport_width") or 1920),
        viewport_height=int(raw.get("viewport_height") or 1080),
        hardware_concurrency=int(raw.get("hardware_concurrency") or 8),
        device_memory=int(raw.get("device_memory") or 8),
        webgl_vendor=str(raw.get("webgl_vendor") or _GPU_PROFILES[0][0]),
        webgl_renderer=str(raw.get("webgl_renderer") or _GPU_PROFILES[0][1]),
        timezone_id=str(raw.get("timezone_id") or timezone_id),
        locale=str(raw.get("locale") or "en-US"),
    )


def _read_fingerprint_store() -> dict[str, Any]:
    if not FINGERPRINT_STORE.exists():
        return {}
    try:
        data = json.loads(FINGERPRINT_STORE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_fingerprint_store(store: dict[str, Any]) -> None:
    FINGERPRINT_STORE.parent.mkdir(parents=True, exist_ok=True)
    FINGERPRINT_STORE.write_text(
        json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _save_fingerprint(fp: FingerprintProfile) -> FingerprintProfile:
    store = _read_fingerprint_store()
    store[fp.key] = asdict(fp)
    store[fp.key]["languages"] = list(fp.languages)
    try:
        _write_fingerprint_store(store)
    except OSError:
        pass
    return fp


def get_stored_fingerprint(
    key: str, *, timezone_id: str = "America/New_York"
) -> FingerprintProfile | None:
    """Return persisted fingerprint for ``key``, or None if missing."""
    store = _read_fingerprint_store()
    raw = store.get(key)
    if isinstance(raw, dict) and raw.get("user_agent"):
        return _fingerprint_from_raw(key, raw, timezone_id=timezone_id)
    return None


def load_or_create_fingerprint(key: str, *, timezone_id: str = "America/New_York") -> FingerprintProfile:
    """Persist fingerprints so the same proxy always gets the same profile."""
    existing = get_stored_fingerprint(key, timezone_id=timezone_id)
    if existing is not None:
        return existing
    return _save_fingerprint(build_fingerprint(key, timezone_id=timezone_id))


def regenerate_fingerprint(
    key: str,
    *,
    timezone_id: str = "America/New_York",
    avoid: FingerprintProfile | None = None,
) -> FingerprintProfile:
    """Force a new fingerprint for ``key`` (overwrites store entry).

    Uses a random salt so the same proxy can leave its previous UA/GPU/viewport.
    """
    previous = avoid
    if previous is None:
        store = _read_fingerprint_store()
        raw = store.get(key)
        if isinstance(raw, dict) and raw.get("user_agent"):
            previous = _fingerprint_from_raw(key, raw, timezone_id=timezone_id)
        else:
            previous = build_fingerprint(key, timezone_id=timezone_id)

    fp = previous
    for _ in range(32):
        salt = f"{key}:regen:{random.randrange(1, 1 << 62)}"
        candidate = build_fingerprint(salt, timezone_id=timezone_id)
        fp = FingerprintProfile(
            key=key,
            seed=candidate.seed,
            user_agent=candidate.user_agent,
            platform=candidate.platform,
            languages=candidate.languages,
            viewport_width=candidate.viewport_width,
            viewport_height=candidate.viewport_height,
            hardware_concurrency=candidate.hardware_concurrency,
            device_memory=candidate.device_memory,
            webgl_vendor=candidate.webgl_vendor,
            webgl_renderer=candidate.webgl_renderer,
            timezone_id=candidate.timezone_id,
            locale=candidate.locale,
        )
        if (
            fp.user_agent != previous.user_agent
            or fp.viewport_width != previous.viewport_width
            or fp.viewport_height != previous.viewport_height
            or fp.webgl_renderer != previous.webgl_renderer
            or fp.seed != previous.seed
        ):
            break
    return _save_fingerprint(fp)


def fingerprint_init_script(fp: FingerprintProfile) -> str:
    """Seeded Canvas/WebGL/Audio/navigator overrides (stable for the same proxy)."""
    langs_json = json.dumps(list(fp.languages))
    return f"""
(() => {{
  const FP = {{
    seed: {int(fp.seed)},
    hw: {int(fp.hardware_concurrency)},
    mem: {int(fp.device_memory)},
    platform: {json.dumps(fp.platform)},
    languages: {langs_json},
    webglVendor: {json.dumps(fp.webgl_vendor)},
    webglRenderer: {json.dumps(fp.webgl_renderer)},
  }};

  // mulberry32 seeded PRNG — same seed => same canvas noise each load
  let _s = FP.seed >>> 0;
  const rnd = () => {{
    _s |= 0; _s = (_s + 0x6D2B79F5) | 0;
    let t = Math.imul(_s ^ (_s >>> 15), 1 | _s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  }};

  try {{
    Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});
    Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => FP.hw }});
    Object.defineProperty(navigator, 'deviceMemory', {{ get: () => FP.mem }});
    Object.defineProperty(navigator, 'platform', {{ get: () => FP.platform }});
    Object.defineProperty(navigator, 'languages', {{ get: () => FP.languages.slice() }});
    Object.defineProperty(navigator, 'language', {{ get: () => FP.languages[0] || 'en-US' }});
    Object.defineProperty(navigator, 'maxTouchPoints', {{ get: () => 0 }});
    window.chrome = window.chrome || {{ runtime: {{}}, app: {{ isInstalled: false }}, csi: () => ({{}}), loadTimes: () => ({{}}) }};
  }} catch (e) {{}}

  const patchCanvas = (proto) => {{
    if (!proto || proto.__fpCanvasPatched) return;
    const orig = proto.toDataURL;
    proto.toDataURL = function(...args) {{
      try {{
        const ctx = this.getContext && this.getContext('2d');
        if (ctx) {{
          const a = Math.floor(rnd() * 3);
          const b = Math.floor(rnd() * 3);
          const c = Math.floor(rnd() * 3);
          ctx.fillStyle = `rgba(${{120 + a}},${{120 + b}},${{120 + c}},0.005)`;
          ctx.fillRect(0, 0, 1, 1);
        }}
      }} catch (e) {{}}
      return orig.apply(this, args);
    }};
    proto.__fpCanvasPatched = true;
  }};
  try {{
    if (typeof HTMLCanvasElement !== 'undefined') patchCanvas(HTMLCanvasElement.prototype);
    if (typeof OffscreenCanvas !== 'undefined') patchCanvas(OffscreenCanvas.prototype);
  }} catch (e) {{}}

  const patchWebGL = (proto) => {{
    if (!proto || proto.__fpWebglPatched) return;
    const orig = proto.getParameter;
    proto.getParameter = function(param) {{
      const UNMASKED_VENDOR = 0x9245;
      const UNMASKED_RENDERER = 0x9246;
      const VENDOR = 0x1F00;
      const RENDERER = 0x1F01;
      if (param === UNMASKED_VENDOR || param === VENDOR) return FP.webglVendor;
      if (param === UNMASKED_RENDERER || param === RENDERER) return FP.webglRenderer;
      return orig.apply(this, arguments);
    }};
    proto.__fpWebglPatched = true;
  }};
  try {{
    if (typeof WebGLRenderingContext !== 'undefined') patchWebGL(WebGLRenderingContext.prototype);
    if (typeof WebGL2RenderingContext !== 'undefined') patchWebGL(WebGL2RenderingContext.prototype);
  }} catch (e) {{}}

  try {{
    if (typeof AnalyserNode !== 'undefined' && AnalyserNode.prototype.getFloatFrequencyData) {{
      const orig = AnalyserNode.prototype.getFloatFrequencyData;
      AnalyserNode.prototype.getFloatFrequencyData = function(arr) {{
        const res = orig.call(this, arr);
        for (let i = 0; i < arr.length; i += Math.max(1, Math.floor(arr.length / 8))) {{
          arr[i] = arr[i] * (0.995 + rnd() * 0.01);
        }}
        return res;
      }};
    }}
  }} catch (e) {{}}
}})();
"""


_mouse_pos: dict[int, tuple[float, float]] = {}


def _bezier(p0: float, p1: float, p2: float, p3: float, t: float) -> float:
    u = 1.0 - t
    return (u**3) * p0 + 3 * (u**2) * t * p1 + 3 * u * (t**2) * p2 + (t**3) * p3


async def human_mouse_move(page, x: float, y: float, *, worker_id: int = 0) -> None:
    """Cubic-bezier mouse path with jitter."""
    if not HUMAN_MOUSE_ENABLED:
        await page.mouse.move(x, y)
        _mouse_pos[worker_id] = (x, y)
        return

    sx, sy = _mouse_pos.get(worker_id, (random.uniform(80, 240), random.uniform(80, 240)))
    dist = math.hypot(x - sx, y - sy)
    steps = max(12, min(48, int(dist / 12) + random.randint(8, 16)))
    cx1 = sx + (x - sx) * random.uniform(0.15, 0.4) + random.uniform(-40, 40)
    cy1 = sy + (y - sy) * random.uniform(0.05, 0.3) + random.uniform(-60, 60)
    cx2 = sx + (x - sx) * random.uniform(0.6, 0.85) + random.uniform(-40, 40)
    cy2 = sy + (y - sy) * random.uniform(0.7, 0.95) + random.uniform(-40, 40)

    for i in range(1, steps + 1):
        t = i / steps
        # ease-in-out
        te = t * t * (3 - 2 * t)
        px = _bezier(sx, cx1, cx2, x, te) + random.uniform(-0.6, 0.6)
        py = _bezier(sy, cy1, cy2, y, te) + random.uniform(-0.6, 0.6)
        await page.mouse.move(px, py)
        await asyncio.sleep(random.uniform(0.004, 0.016))
    await page.mouse.move(x, y)
    _mouse_pos[worker_id] = (x, y)


async def human_click_xy(page, x: float, y: float, *, worker_id: int = 0) -> None:
    await human_mouse_move(page, x, y, worker_id=worker_id)
    await asyncio.sleep(random.uniform(0.05, 0.18))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.04, 0.12))
    await page.mouse.up()
    await asyncio.sleep(random.uniform(0.08, 0.25))


async def human_click_locator(page, locator, *, worker_id: int = 0, timeout: float = 2000) -> bool:
    try:
        box = await locator.bounding_box(timeout=timeout)
        if not box:
            return False
        x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
        await human_click_xy(page, x, y, worker_id=worker_id)
        return True
    except Exception:
        try:
            await locator.click(timeout=timeout)
            return True
        except Exception:
            return False


async def human_scroll(page, delta_y: int, *, worker_id: int = 0) -> None:
    """Scroll in several smaller wheel ticks with mouse idle moves."""
    if not HUMAN_MOUSE_ENABLED:
        await page.mouse.wheel(0, delta_y)
        return

    remaining = abs(delta_y)
    direction = 1 if delta_y >= 0 else -1
    sx, sy = _mouse_pos.get(worker_id, (random.uniform(200, 600), random.uniform(200, 500)))
    await human_mouse_move(
        page,
        sx + random.uniform(-30, 30),
        sy + random.uniform(-20, 40),
        worker_id=worker_id,
    )
    while remaining > 0:
        step = min(remaining, random.randint(180, 420))
        await page.mouse.wheel(0, step * direction)
        remaining -= step
        await asyncio.sleep(random.uniform(0.12, 0.35))
        # slight horizontal wander while scrolling
        cx, cy = _mouse_pos.get(worker_id, (sx, sy))
        await human_mouse_move(
            page,
            cx + random.uniform(-25, 25),
            cy + random.uniform(-15, 25),
            worker_id=worker_id,
        )


async def human_idle(page, *, worker_id: int = 0, seconds: float | None = None) -> None:
    delay = seconds if seconds is not None else random.uniform(0.6, 1.6)
    end = asyncio.get_event_loop().time() + delay
    while asyncio.get_event_loop().time() < end:
        cx, cy = _mouse_pos.get(worker_id, (400.0, 300.0))
        await human_mouse_move(
            page,
            cx + random.uniform(-50, 50),
            cy + random.uniform(-30, 30),
            worker_id=worker_id,
        )
        await asyncio.sleep(random.uniform(0.15, 0.4))


async def apply_stealth_and_fingerprint(context, page, fp: FingerprintProfile | None) -> str:
    """Apply playwright-stealth + seeded fingerprint scripts. Returns mode label."""
    labels: list[str] = []

    if STEALTH_ENABLED:
        try:
            from playwright_stealth import Stealth

            stealth_kwargs: dict[str, Any] = {
                "navigator_languages_override": (
                    tuple(fp.languages) if fp else ("en-US", "en")
                ),
                "navigator_platform_override": fp.platform if fp else "Win32",
                "chrome_runtime": False,
            }
            if fp:
                stealth_kwargs["navigator_user_agent_override"] = fp.user_agent
                stealth_kwargs["webgl_vendor_override"] = fp.webgl_vendor
                stealth_kwargs["webgl_renderer_override"] = fp.webgl_renderer
            stealth = Stealth(**stealth_kwargs)
            await stealth.apply_stealth_async(context)
            labels.append("playwright-stealth")
        except Exception as exc:
            labels.append(f"playwright-stealth-fail:{exc}")

    if FINGERPRINT_ENABLED and fp is not None:
        try:
            await context.add_init_script(fingerprint_init_script(fp))
            # Also patch already-open pages.
            if page is not None:
                try:
                    await page.add_init_script(fingerprint_init_script(fp))
                except Exception:
                    pass
            labels.append(f"fingerprint-pool({fp.key})")
        except Exception as exc:
            labels.append(f"fingerprint-fail:{exc}")

    # Extra pass from pw-stealth-enhanced for fonts/audio base (optional, non-fatal).
    if STEALTH_ENABLED:
        try:
            from pw_stealth_enhanced import apply_stealth

            await apply_stealth(
                context,
                user_agent=fp.user_agent if fp else None,
                viewport=(
                    {"width": fp.viewport_width, "height": fp.viewport_height} if fp else None
                ),
                locale=fp.locale if fp else "en-US",
                timezone_id=fp.timezone_id if fp else "America/New_York",
            )
            # Re-apply seeded fingerprint AFTER enhanced so our seeded canvas wins.
            if FINGERPRINT_ENABLED and fp is not None:
                await context.add_init_script(fingerprint_init_script(fp))
            labels.append("pw-stealth-enhanced")
        except Exception:
            pass

    return "+".join(labels) if labels else "none"
