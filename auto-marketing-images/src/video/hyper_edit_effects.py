"""Catálogo de efeitos hyper-edit — 16 efeitos, todos com movimento de câmera virtual.

Inspirado em OpusClip / Velocity Edit / Hyper-Edit:
- Efeitos de câmera primários: zoom, pan, rotação, dolly, shake, spring physics
- Efeitos de cor/luz: sempre combinados com camada de movimento de base

Todos os efeitos preservam as dimensões originais do clip (w × h).
"""
from __future__ import annotations

import math
import random

import numpy as np

EFFECT_CATALOGUE = [
    # --- Câmera / movimento primário ---
    "parallax_zoom",     # Ken Burns: zoom in + pan diagonal agressivo
    "corner_zoom_pan",   # Zoom top-right → zoom-out → zoom bottom-left
    "cinematic_dolly",   # Push-in lento estilo cinema
    "zoom_pulse",        # Pulso gaussiano de escala no 1/3 da duração
    "heartbeat_pulse",   # Dois pulsos de escala (batimento cardíaco)
    "bounce_zoom",       # Zoom com spring physics (amortecimento)
    "whip_zoom",         # Zoom rápido in → recuo suave
    "drift_float",       # Flutuação diagonal com ease-in-out
    # --- Câmera + rotação ---
    "rotation_drift",    # Giro leve ±3° + zoom (anti-bordo automático)
    "rotation_bounce",   # Giro oscila ida-e-volta + zoom pulse
    # --- Shake / agitação ---
    "camera_shake",      # Tremor pesado por frame com rajadas
    "pendulum_shake",    # Shake sinusoidal suave com decaimento
    # --- Cor/luz + camada de movimento ---
    "exposure_flash",    # Flash branco + zoom punch de entrada
    "flicker_strobe",    # Estroboscópio + shake pendular
    "rgb_glow",          # Aberração cromática + zoom drift
    "deep_fried",        # Saturação/contraste extremos + zoom pulse
]


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _resize_frame(frame: np.ndarray, new_w: int, new_h: int) -> np.ndarray:
    """Redimensiona frame numpy via PIL BILINEAR; fallback para interpolação simples."""
    try:
        from PIL import Image as _PIL
        img = _PIL.fromarray(frame)
        img = img.resize((new_w, new_h), _PIL.BILINEAR)
        return np.array(img)
    except Exception:  # noqa: BLE001
        fh, fw = frame.shape[:2]
        if new_w == fw and new_h == fh:
            return frame
        xs = np.linspace(0, fw - 1, new_w).astype(np.int32)
        ys = np.linspace(0, fh - 1, new_h).astype(np.int32)
        return frame[np.ix_(ys, xs)]


def _rotate_frame(frame: np.ndarray, angle_deg: float) -> np.ndarray:
    """Rotaciona frame via PIL; fillcolor = preto (bordas cobertas por zoom)."""
    try:
        from PIL import Image as _PIL
        img = _PIL.fromarray(frame)
        rotated = img.rotate(angle_deg, resample=_PIL.BILINEAR, expand=False, fillcolor=(0, 0, 0))
        return np.array(rotated)
    except Exception:  # noqa: BLE001
        return frame


def _easing_smooth(t: float) -> float:
    """Ease in-out cúbico (0→1)."""
    return t * t * (3 - 2 * t)


def _crop_center(frame: np.ndarray, out_w: int, out_h: int) -> np.ndarray:
    """Crop centralizado de frame (frame pode ser maior que out_w×out_h)."""
    fh, fw = frame.shape[:2]
    x1 = max(0, (fw - out_w) // 2)
    y1 = max(0, (fh - out_h) // 2)
    return frame[y1: y1 + out_h, x1: x1 + out_w]


def _zoom_and_crop(frame: np.ndarray, scale: float, anchor_x: float, anchor_y: float,
                   out_w: int, out_h: int) -> np.ndarray:
    """Redimensiona frame para scale×(out_w, out_h) e recorta a partir de (anchor_x, anchor_y).

    anchor_x/y em [0,1]: 0.0=esquerda/topo, 0.5=centro, 1.0=direita/baixo.
    """
    new_w = max(out_w, int(round(out_w * scale)))
    new_h = max(out_h, int(round(out_h * scale)))
    resized = _resize_frame(frame, new_w, new_h)
    dx = int((new_w - out_w) * anchor_x)
    dy = int((new_h - out_h) * anchor_y)
    x1 = max(0, min(dx, new_w - out_w))
    y1 = max(0, min(dy, new_h - out_h))
    return resized[y1: y1 + out_h, x1: x1 + out_w]


# ---------------------------------------------------------------------------
# Câmera / movimento primário
# ---------------------------------------------------------------------------

def _parallax_zoom(clip, fps: int):
    """Ken Burns agressivo: zoom 12–22% + pan diagonal com âncora aleatória."""
    duration = float(clip.duration)
    zoom = random.uniform(0.14, 0.24)
    # Âncora inicial e final aleatórias para cada clip (garante variação)
    ax0 = random.uniform(0.1, 0.9)
    ay0 = random.uniform(0.1, 0.9)
    ax1 = random.uniform(0.1, 0.9)
    ay1 = random.uniform(0.1, 0.9)
    w, h = clip.size

    def fl_img(frame: np.ndarray, t: float) -> np.ndarray:
        p = _easing_smooth(t / max(duration, 1e-6))
        scale = 1.0 + zoom * p
        ax = ax0 + (ax1 - ax0) * p
        ay = ay0 + (ay1 - ay0) * p
        return _zoom_and_crop(frame, scale, ax, ay, w, h)

    return clip.fl(lambda gf, t: fl_img(gf(t), t), apply_to="video")


def _corner_zoom_pan(clip, fps: int):
    """Zoom in num canto → zoom-out ao centro → zoom in no canto oposto.

    Par de cantos escolhido aleatoriamente a cada chamada (4 pares possíveis).
    Intensidade do zoom em cada fase também é aleatória.
    """
    duration = float(clip.duration)
    w, h = clip.size

    # 4 pares de cantos opostos: (start_ax, start_ay, end_ax, end_ay)
    _corner_pairs = [
        (1.0, 0.0, 0.0, 1.0),  # top-right → bottom-left
        (0.0, 0.0, 1.0, 1.0),  # top-left → bottom-right
        (1.0, 1.0, 0.0, 0.0),  # bottom-right → top-left
        (0.0, 1.0, 1.0, 0.0),  # bottom-left → top-right
    ]
    sax, say, eax, eay = random.choice(_corner_pairs)
    zoom1 = random.uniform(1.25, 1.45)   # escala máxima na fase 1
    zoom3 = random.uniform(1.18, 1.35)   # escala máxima na fase 3

    def fl_img(frame: np.ndarray, t: float) -> np.ndarray:
        p = t / max(duration, 1e-6)

        if p <= 0.45:
            # Fase 1: zoom in no canto inicial → zoom-out ao centro
            phase = _easing_smooth(p / 0.45)
            scale = zoom1 - (zoom1 - 1.0) * phase
            ax = sax + (0.5 - sax) * phase
            ay = say + (0.5 - say) * phase
        elif p <= 0.55:
            # Fase 2: pausa breve no centro com zoom mínimo
            scale = 1.0
            ax = 0.5
            ay = 0.5
        else:
            # Fase 3: zoom in no canto oposto
            phase = _easing_smooth((p - 0.55) / 0.45)
            scale = 1.0 + (zoom3 - 1.0) * phase
            ax = 0.5 + (eax - 0.5) * phase
            ay = 0.5 + (eay - 0.5) * phase

        return _zoom_and_crop(frame, scale, ax, ay, w, h)

    return clip.fl(lambda gf, t: fl_img(gf(t), t), apply_to="video")


def _cinematic_dolly(clip, fps: int):
    """Dolly lento estilo cinema: push-in suave 6–14% com pan mínimo."""
    duration = float(clip.duration)
    zoom = random.uniform(0.06, 0.14)
    # Pan muito sutil — dolly de cinema não vai para o lado
    pan_x = random.uniform(-0.02, 0.02)
    pan_y = random.uniform(-0.02, 0.02)
    w, h = clip.size

    def fl_img(frame: np.ndarray, t: float) -> np.ndarray:
        p = t / max(duration, 1e-6)           # linear (dolly real não tem easing)
        scale = 1.0 + zoom * p
        ax = 0.5 + pan_x * p
        ay = 0.5 + pan_y * p
        return _zoom_and_crop(frame, scale, ax, ay, w, h)

    return clip.fl(lambda gf, t: fl_img(gf(t), t), apply_to="video")


def _zoom_pulse(clip, fps: int):
    """Pulso gaussiano de escala 15–20% no 1/3 da duração."""
    duration = float(clip.duration)
    peak_t = duration / 3.0
    peak = random.uniform(0.15, 0.22)
    sigma = duration / 5.0
    # Âncora aleatória: o zoom vai para um dos cantos
    ax = random.uniform(0.2, 0.8)
    ay = random.uniform(0.2, 0.8)
    w, h = clip.size

    def fl_img(frame: np.ndarray, t: float) -> np.ndarray:
        scale = 1.0 + peak * math.exp(-((t - peak_t) ** 2) / (2 * sigma ** 2))
        return _zoom_and_crop(frame, scale, ax, ay, w, h)

    return clip.fl(lambda gf, t: fl_img(gf(t), t), apply_to="video")


def _heartbeat_pulse(clip, fps: int):
    """Dois pulsos de zoom — estilo batimento cardíaco (pulso 1 maior que o 2)."""
    duration = float(clip.duration)
    p1_t = duration * random.uniform(0.22, 0.35)
    p2_t = duration * random.uniform(0.58, 0.78)
    sigma = duration * random.uniform(0.06, 0.10)
    ax = random.uniform(0.3, 0.7)
    ay = random.uniform(0.3, 0.7)
    w, h = clip.size

    def fl_img(frame: np.ndarray, t: float) -> np.ndarray:
        g1 = 0.22 * math.exp(-((t - p1_t) ** 2) / (2 * sigma ** 2))
        g2 = 0.14 * math.exp(-((t - p2_t) ** 2) / (2 * sigma ** 2))
        scale = 1.0 + max(g1, g2)
        return _zoom_and_crop(frame, scale, ax, ay, w, h)

    return clip.fl(lambda gf, t: fl_img(gf(t), t), apply_to="video")


def _bounce_zoom(clip, fps: int):
    """Zoom com spring physics: sobe rápido até o pico e oscila amortecendo."""
    duration = float(clip.duration)
    peak = random.uniform(0.28, 0.40)   # amplitude do zoom
    decay = random.uniform(6.0, 9.0)    # amortecimento
    freq = random.uniform(2.5, 4.0)     # frequência de oscilação
    ax = random.uniform(0.3, 0.7)
    ay = random.uniform(0.3, 0.7)
    w, h = clip.size

    def fl_img(frame: np.ndarray, t: float) -> np.ndarray:
        # Oscilação amortecida: pico imediato, decai e oscila
        osc = math.exp(-decay * t) * abs(math.cos(2 * math.pi * freq * t))
        scale = max(1.0, 1.0 + peak * osc)
        return _zoom_and_crop(frame, scale, ax, ay, w, h)

    return clip.fl(lambda gf, t: fl_img(gf(t), t), apply_to="video")


def _whip_zoom(clip, fps: int):
    """Zoom muito rápido in nos primeiros 20% → recuo gradual até 1.05×."""
    duration = float(clip.duration)
    peak = random.uniform(1.40, 1.55)   # escala no pico
    ax = random.uniform(0.2, 0.8)
    ay = random.uniform(0.2, 0.8)
    w, h = clip.size

    def _ease_out_cubic(x: float) -> float:
        return 1 - (1 - x) ** 3

    def fl_img(frame: np.ndarray, t: float) -> np.ndarray:
        p = t / max(duration, 1e-6)
        if p <= 0.20:
            phase = p / 0.20
            scale = 1.0 + (peak - 1.0) * _ease_out_cubic(phase)
        else:
            phase = (p - 0.20) / 0.80
            scale = peak - (peak - 1.05) * _easing_smooth(phase)
        return _zoom_and_crop(frame, max(1.0, scale), ax, ay, w, h)

    return clip.fl(lambda gf, t: fl_img(gf(t), t), apply_to="video")


def _drift_float(clip, fps: int):
    """Flutuação diagonal suave com ease-in-out: câmera 'flutua' por 25px e faz zoom leve."""
    duration = float(clip.duration)
    w, h = clip.size
    pad = 32
    dx_total = random.choice([-1, 1]) * random.uniform(14, 26)
    dy_total = random.choice([-1, 1]) * random.uniform(10, 20)
    zoom_end = random.uniform(1.06, 1.14)

    def fl_img(frame: np.ndarray, t: float) -> np.ndarray:
        p = _easing_smooth(t / max(duration, 1e-6))
        scale = 1.0 + (zoom_end - 1.0) * p
        new_w = int(round(w * scale))
        new_h = int(round(h * scale))
        resized = _resize_frame(frame, new_w, new_h)
        cx = (new_w - w) // 2 + int(dx_total * p)
        cy = (new_h - h) // 2 + int(dy_total * p)
        x1 = max(0, min(cx, new_w - w))
        y1 = max(0, min(cy, new_h - h))
        return resized[y1: y1 + h, x1: x1 + w]

    return clip.fl(lambda gf, t: fl_img(gf(t), t), apply_to="video")


# ---------------------------------------------------------------------------
# Câmera + rotação
# ---------------------------------------------------------------------------

def _rotation_drift(clip, fps: int):
    """Giro leve ±2–4° + zoom 10% para cobrir bordas. Rotação segue curva senoidal."""
    duration = float(clip.duration)
    angle_amp = random.uniform(2.0, 4.5)
    direction = random.choice([-1, 1])
    # Zoom mínimo para esconder cantos escuros causados pela rotação
    zoom_rot = 1.12
    # Adiciona movimento de pan junto com a rotação
    ax0 = random.uniform(0.35, 0.65)
    ay0 = random.uniform(0.35, 0.65)
    ax1 = random.uniform(0.35, 0.65)
    ay1 = random.uniform(0.35, 0.65)
    w, h = clip.size

    def fl_img(frame: np.ndarray, t: float) -> np.ndarray:
        p = t / max(duration, 1e-6)
        angle = direction * angle_amp * math.sin(math.pi * p)
        scale = zoom_rot + 0.04 * p         # cresce levemente ao longo do clip
        ax = ax0 + (ax1 - ax0) * p
        ay = ay0 + (ay1 - ay0) * p

        new_w = int(round(w * scale))
        new_h = int(round(h * scale))
        resized = _resize_frame(frame, new_w, new_h)
        rotated = _rotate_frame(resized, angle)
        x1 = (new_w - w) // 2 + int((new_w - w) * (ax - 0.5))
        y1 = (new_h - h) // 2 + int((new_h - h) * (ay - 0.5))
        x1 = max(0, min(x1, new_w - w))
        y1 = max(0, min(y1, new_h - h))
        return rotated[y1: y1 + h, x1: x1 + w]

    return clip.fl(lambda gf, t: fl_img(gf(t), t), apply_to="video")


def _rotation_bounce(clip, fps: int):
    """Rotação que oscila ida-e-volta (±5°) enquanto o zoom pulsa. Estilo 'snap' de edição."""
    duration = float(clip.duration)
    angle_amp = random.uniform(3.5, 6.0)
    freq_rot = random.uniform(0.8, 1.2)    # ~1 ciclo completo durante o slide
    zoom_base = 1.14                       # zoom mínimo para cobrir bordas
    zoom_pulse_amp = random.uniform(0.08, 0.14)
    peak_t = duration * 0.35
    sigma = duration / 6.0
    w, h = clip.size

    def fl_img(frame: np.ndarray, t: float) -> np.ndarray:
        p = t / max(duration, 1e-6)
        angle = angle_amp * math.sin(2 * math.pi * freq_rot * p)
        g_zoom = zoom_pulse_amp * math.exp(-((t - peak_t) ** 2) / (2 * sigma ** 2))
        scale = zoom_base + g_zoom

        new_w = int(round(w * scale))
        new_h = int(round(h * scale))
        resized = _resize_frame(frame, new_w, new_h)
        rotated = _rotate_frame(resized, angle)
        x1 = (new_w - w) // 2
        y1 = (new_h - h) // 2
        return rotated[y1: y1 + h, x1: x1 + w]

    return clip.fl(lambda gf, t: fl_img(gf(t), t), apply_to="video")


# ---------------------------------------------------------------------------
# Shake / agitação
# ---------------------------------------------------------------------------

def _camera_shake(clip, fps: int):
    """Tremor pesado: ±16px por frame, rajadas a cada 3 frames para simular 'hit' de beat."""
    w, h = clip.size
    pad = random.randint(14, 24)
    burst_mult = random.uniform(1.5, 2.2)
    rng = np.random.default_rng(seed=random.randint(0, 9999))
    total_frames = max(1, int(float(clip.duration) * fps) + 10)
    raw_x = rng.integers(-pad, pad + 1, size=total_frames)
    raw_y = rng.integers(-pad, pad + 1, size=total_frames)
    for fi in range(0, total_frames, 3):
        raw_x[fi] = int(np.clip(raw_x[fi] * burst_mult, -pad, pad))
        raw_y[fi] = int(np.clip(raw_y[fi] * burst_mult, -pad, pad))
    offsets_x = np.clip(raw_x, -pad, pad)
    offsets_y = np.clip(raw_y, -pad, pad)

    def fl_img(frame: np.ndarray, t: float) -> np.ndarray:
        fi = min(int(t * fps), total_frames - 1)
        ox, oy = int(offsets_x[fi]), int(offsets_y[fi])
        resized = _resize_frame(frame, w + pad * 2, h + pad * 2)
        x1 = max(0, min(pad + ox, pad * 2))
        y1 = max(0, min(pad + oy, pad * 2))
        return resized[y1: y1 + h, x1: x1 + w]

    return clip.fl(lambda gf, t: fl_img(gf(t), t), apply_to="video")


def _pendulum_shake(clip, fps: int):
    """Shake sinusoidal suave com decaimento (pendulo amortecido) — sem cut repentino."""
    w, h = clip.size
    duration = float(clip.duration)
    pad = 24
    freq = random.uniform(2.8, 4.2)
    amp_x = random.uniform(10, 18)
    amp_y = random.uniform(5, 10)
    # Decaimento leve: começa mais forte e suaviza no final
    decay = 0.8 / max(duration, 1e-6)

    def fl_img(frame: np.ndarray, t: float) -> np.ndarray:
        env = math.exp(-decay * t)
        ox = int(amp_x * math.sin(2 * math.pi * freq * t) * env)
        oy = int(amp_y * math.cos(2 * math.pi * freq * t * 0.65) * env)
        resized = _resize_frame(frame, w + pad * 2, h + pad * 2)
        x1 = max(0, min(pad + ox, pad * 2))
        y1 = max(0, min(pad + oy, pad * 2))
        return resized[y1: y1 + h, x1: x1 + w]

    return clip.fl(lambda gf, t: fl_img(gf(t), t), apply_to="video")


# ---------------------------------------------------------------------------
# Cor/luz + camada de movimento
# ---------------------------------------------------------------------------

def _exposure_flash(clip, fps: int):
    """Flash branco decaindo nos primeiros ~0.6s + zoom punch de entrada (whip rápido)."""
    flash_frames = random.randint(12, 22)    # ~0.4–0.73s a 30fps
    duration = float(clip.duration)
    # Zoom punch: entra rápido em 0.25s e recua
    peak_scale = random.uniform(1.20, 1.30)
    punch_duration = min(0.25, duration * 0.35)
    ax = random.uniform(0.3, 0.7)
    ay = random.uniform(0.3, 0.7)
    w, h = clip.size

    def fl_img(frame: np.ndarray, t: float) -> np.ndarray:
        fi = int(t * 30)

        # --- Camada de zoom punch ---
        if t <= punch_duration:
            phase = t / punch_duration
            scale = 1.0 + (peak_scale - 1.0) * (1 - (1 - phase) ** 3)
        else:
            p = (t - punch_duration) / max(duration - punch_duration, 1e-6)
            scale = peak_scale - (peak_scale - 1.05) * _easing_smooth(p)
        frame = _zoom_and_crop(frame, max(1.0, scale), ax, ay, w, h)

        # --- Camada de flash branco ---
        if fi < flash_frames:
            alpha = (1.0 - fi / flash_frames) ** 2
            img = frame.astype(np.float32)
            frame = np.clip(img + (255.0 - img) * alpha, 0, 255).astype(np.uint8)

        return frame

    return clip.fl(lambda gf, t: fl_img(gf(t), t), apply_to="video")


def _flicker_strobe(clip, fps: int):
    """Estroboscópio intenso + shake pendular — câmera treme junto com o pulso de luz."""
    w, h = clip.size
    duration = float(clip.duration)
    freq_strobe = random.uniform(8.0, 15.0)
    strobe_amp = random.uniform(0.22, 0.38)
    pad = 14
    freq_shake = random.uniform(8.0, 12.0)
    amp = random.uniform(6, 12)

    def fl_img(frame: np.ndarray, t: float) -> np.ndarray:
        # --- Camada de shake ---
        ox = int(amp * math.sin(2 * math.pi * freq_shake * t))
        oy = int(amp * 0.5 * math.cos(2 * math.pi * freq_shake * t * 0.7))
        resized = _resize_frame(frame, w + pad * 2, h + pad * 2)
        x1 = max(0, min(pad + ox, pad * 2))
        y1 = max(0, min(pad + oy, pad * 2))
        frame_shaken = resized[y1: y1 + h, x1: x1 + w]

        # --- Camada de strobe ---
        brightness = (1.0 - strobe_amp) + strobe_amp * math.sin(2 * math.pi * freq_strobe * t)
        img = frame_shaken.astype(np.float32) * brightness
        return np.clip(img, 0, 255).astype(np.uint8)

    return clip.fl(lambda gf, t: fl_img(gf(t), t), apply_to="video")


def _rgb_glow(clip, fps: int):
    """Aberração cromática animada (shift aumenta/diminui) + zoom drift leve."""
    duration = float(clip.duration)
    max_shift = random.randint(6, 14)
    zoom_end = random.uniform(1.06, 1.12)
    ax = random.uniform(0.3, 0.7)
    ay = random.uniform(0.3, 0.7)
    w, h = clip.size
    border = random.randint(20, 38)

    def fl_img(frame: np.ndarray, t: float) -> np.ndarray:
        p = t / max(duration, 1e-6)

        # --- Camada de zoom drift ---
        frame = _zoom_and_crop(frame, 1.0 + (zoom_end - 1.0) * _easing_smooth(p), ax, ay, w, h)

        # --- Aberração cromática animada ---
        if frame.ndim < 3 or frame.shape[2] < 3:
            return frame
        shift = int(max_shift * (0.5 + 0.5 * math.sin(2 * math.pi * 0.8 * t)))
        fh, fw = frame.shape[:2]
        out = np.zeros_like(frame)
        # R: shift para a direita
        if shift > 0:
            out[:, shift:, 0] = frame[:, : fw - shift, 0]
        else:
            out[:, :, 0] = frame[:, :, 0]
        # G: no lugar
        out[:, :, 1] = frame[:, :, 1]
        # B: shift para a esquerda
        if shift > 0:
            out[:, : fw - shift, 2] = frame[:, shift:, 2]
        else:
            out[:, :, 2] = frame[:, :, 2]
        if frame.shape[2] > 3:
            out[:, :, 3:] = frame[:, :, 3:]

        # Brilho de borda (glow de neon)
        glow = np.zeros((fh, fw), dtype=np.float32)
        glow[:border, :] = np.linspace(0.45, 0, border)[:, None]
        glow[-border:, :] = np.linspace(0, 0.45, border)[:, None]
        glow[:, :border] = np.maximum(glow[:, :border], np.linspace(0.45, 0, border)[None, :])
        glow[:, -border:] = np.maximum(glow[:, -border:], np.linspace(0, 0.45, border)[None, :])
        glow = np.clip(glow, 0, 0.55)[:, :, None]
        out_f = out[:, :, :3].astype(np.float32)
        out_f = out_f + glow * (255.0 - out_f)
        out[:, :, :3] = np.clip(out_f, 0, 255).astype(np.uint8)
        return out

    return clip.fl(lambda gf, t: fl_img(gf(t), t), apply_to="video")


def _deep_fried(clip, fps: int):
    """Saturação ×2.2 + contraste ×1.7 ("queimado") + zoom pulse no pico."""
    sat = random.uniform(1.8, 2.5)
    contrast = random.uniform(1.4, 2.0)
    duration = float(clip.duration)
    peak_t = duration * 0.30
    sigma = duration / 6.0
    zoom_peak = random.uniform(0.12, 0.18)
    ax = random.uniform(0.3, 0.7)
    ay = random.uniform(0.3, 0.7)
    w, h = clip.size

    def fl_img(frame: np.ndarray, t: float) -> np.ndarray:
        # --- Camada de zoom pulse ---
        g = zoom_peak * math.exp(-((t - peak_t) ** 2) / (2 * sigma ** 2))
        scale = 1.0 + g
        frame = _zoom_and_crop(frame, scale, ax, ay, w, h)

        # --- Cor "deep fried" ---
        img = frame.astype(np.float32)
        if img.ndim >= 3 and img.shape[2] >= 3:
            rgb = img[:, :, :3]
            grey = np.mean(rgb, axis=2, keepdims=True)
            saturated = grey + sat * (rgb - grey)
            mid = 128.0
            contrasted = mid + contrast * (saturated - mid)
            if img.shape[2] > 3:
                result = np.concatenate([contrasted, img[:, :, 3:]], axis=2)
            else:
                result = contrasted
            return np.clip(result, 0, 255).astype(np.uint8)
        return frame

    return clip.fl(lambda gf, t: fl_img(gf(t), t), apply_to="video")


# ---------------------------------------------------------------------------
# Glitch (era separado, agora incorporado no catálogo legacy para compatibilidade)
# ---------------------------------------------------------------------------

def _glitch_cut(clip, fps: int):
    """Glitch digital: fatias deslocadas ±35px em dois momentos + shake de base."""
    duration = float(clip.duration)
    mid_t = duration / 2.0
    end_t = duration - 4.0 / max(fps, 1)
    half_win = 5.0 / max(fps, 1)
    rng = np.random.default_rng(seed=random.randint(0, 999))
    slice_count = random.randint(7, 15)
    pad = 12
    amp_shake = random.randint(7, 14)
    w, h = clip.size

    def _apply_glitch(frame: np.ndarray) -> np.ndarray:
        fh, fw = frame.shape[:2]
        out = frame.copy()
        slice_h = max(1, fh // slice_count)
        for s in range(slice_count):
            shift = int(rng.integers(-35, 36))
            y0 = s * slice_h
            y1 = min(y0 + slice_h, fh)
            out[y0:y1, :, :] = np.roll(frame[y0:y1, :, :], shift, axis=1)
        return out

    def fl_img(frame: np.ndarray, t: float) -> np.ndarray:
        # Shake de base constante
        ox = int(amp_shake * math.sin(2 * math.pi * 5.0 * t))
        oy = int(amp_shake * 0.5 * math.cos(2 * math.pi * 3.5 * t))
        resized = _resize_frame(frame, w + pad * 2, h + pad * 2)
        x1 = max(0, min(pad + ox, pad * 2))
        y1_c = max(0, min(pad + oy, pad * 2))
        frame_shaken = resized[y1_c: y1_c + h, x1: x1 + w]

        if abs(t - mid_t) <= half_win or t >= end_t:
            return _apply_glitch(frame_shaken)
        return frame_shaken

    return clip.fl(lambda gf, t: fl_img(gf(t), t), apply_to="video")


# ---------------------------------------------------------------------------
# Dispatcher público
# ---------------------------------------------------------------------------

_HANDLERS: dict[str, object] = {
    "parallax_zoom": _parallax_zoom,
    "corner_zoom_pan": _corner_zoom_pan,
    "cinematic_dolly": _cinematic_dolly,
    "zoom_pulse": _zoom_pulse,
    "heartbeat_pulse": _heartbeat_pulse,
    "bounce_zoom": _bounce_zoom,
    "whip_zoom": _whip_zoom,
    "drift_float": _drift_float,
    "rotation_drift": _rotation_drift,
    "rotation_bounce": _rotation_bounce,
    "camera_shake": _camera_shake,
    "pendulum_shake": _pendulum_shake,
    "exposure_flash": _exposure_flash,
    "flicker_strobe": _flicker_strobe,
    "rgb_glow": _rgb_glow,
    "deep_fried": _deep_fried,
    # alias legado
    "glitch_cut": _glitch_cut,
}


def apply_effect(clip, effect_id: str, fps: int = 30):
    """Aplica o efeito indicado a um ImageClip com duração definida.

    Retorna o clip modificado; se o effect_id for desconhecido, retorna sem alteração.
    """
    fn = _HANDLERS.get(effect_id)
    if fn is None:
        return clip
    try:
        return fn(clip, fps)  # type: ignore[operator]
    except Exception as exc:  # noqa: BLE001
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "Efeito %r falhou; clip sem efeito. Erro: %s", effect_id, exc
        )
        return clip
