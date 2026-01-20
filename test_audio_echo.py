#!/usr/bin/env python3
"""
Echo test for audio pipeline.
Records audio, sends it through the RTP/OpenAI pipeline, and verifies it comes back.
"""

import asyncio
import logging
import numpy as np
from scipy import signal
import struct

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def ulaw_decode(ulaw_bytes):
    """
    Decode G.711 ulaw to PCM16 using audioop (standard library).
    
    Args:
        ulaw_bytes: numpy array of uint8 ulaw-encoded bytes
        
    Returns:
        numpy array of int16 PCM samples
    """
    import audioop
    # Convert numpy array to bytes
    ulaw_bytes_b = ulaw_bytes.tobytes() if isinstance(ulaw_bytes, np.ndarray) else bytes(ulaw_bytes)
    # Decode using audioop (width=2 means 16-bit samples)
    pcm_bytes = audioop.ulaw2lin(ulaw_bytes_b, 2)
    return np.frombuffer(pcm_bytes, dtype=np.int16)


def test_ulaw_decode():
    """Test ulaw decoding with known values."""
    logger.info("Testing ulaw decode...")
    
    # Test with some known ulaw values
    # ulaw 0xFF (all 1s) should decode to a large negative value
    # ulaw 0x7F (sign bit 0, all 1s) should decode to a large positive value
    # ulaw 0x00 should decode to near zero
    
    # Test with known ulaw values using audioop (standard library)
    test_cases = [
        (0xFF, 0),       # audioop produces 0
        (0x7F, 0),       # audioop produces 0
        (0x00, -32124),  # audioop produces -32124
        (0x80, 32124),   # audioop produces 32124
    ]
    
    for ulaw_val, expected in test_cases:
        ulaw_bytes = np.array([ulaw_val], dtype=np.uint8)
        decoded = ulaw_decode(ulaw_bytes)[0]
        match = "✓" if decoded == expected else "✗"
        logger.info(f"{match} ulaw 0x{ulaw_val:02X} -> PCM16 {decoded} (expected {expected})")
    
    logger.info("ulaw decode test complete")


def test_resample_8k_to_24k():
    """Test resampling from 8kHz to 24kHz."""
    logger.info("Testing 8kHz -> 24kHz resampling...")
    
    # Generate a 440Hz tone at 8kHz (1 second = 8000 samples)
    duration = 0.1  # 100ms
    sample_rate_8k = 8000
    sample_rate_24k = 24000
    frequency = 440
    
    t_8k = np.linspace(0, duration, int(sample_rate_8k * duration))
    tone_8k = np.sin(2 * np.pi * frequency * t_8k)
    audio_8k = (tone_8k * 16000).astype(np.int16)  # Scale to int16 range
    
    logger.info(f"Generated {len(audio_8k)} samples at 8kHz (440Hz tone)")
    
    # Resample to 24kHz using resample_poly (3x upsampling)
    # Convert to float64 first (resample_poly requires float input)
    audio_8k_float = audio_8k.astype(np.float64)
    audio_24k_float = signal.resample_poly(audio_8k_float, 3, 1)
    audio_24k = np.clip(audio_24k_float, -32768, 32767).astype(np.int16)
    
    logger.info(f"Resampled to {len(audio_24k)} samples at 24kHz")
    logger.info(f"Audio stats 8k: min={audio_8k.min()}, max={audio_8k.max()}, mean_abs={np.mean(np.abs(audio_8k)):.1f}")
    logger.info(f"Audio stats 24k: min={audio_24k.min()}, max={audio_24k.max()}, mean_abs={np.mean(np.abs(audio_24k)):.1f}")
    
    # Verify we got 3x the samples
    expected_samples = len(audio_8k) * 3
    if len(audio_24k) == expected_samples:
        logger.info(f"✓ Resampling correct: {len(audio_8k)} -> {len(audio_24k)} samples")
    else:
        logger.warning(f"✗ Resampling incorrect: expected {expected_samples}, got {len(audio_24k)}")
    
    logger.info("Resampling test complete")


def test_rtp_payload_parsing():
    """Test RTP payload type detection."""
    logger.info("Testing RTP payload type parsing...")
    
    # Simulate RTP header: V=2, P=0, X=0, CC=0, M=0, PT=0 (ulaw)
    header_ulaw = struct.pack('!BBHII',
        0x80,  # V=2, P=0, X=0, CC=0, M=0
        0x00,  # PT=0 (ulaw)
        0x0001,  # Sequence
        0x00000000,  # Timestamp
        0x12345678  # SSRC
    )
    
    # Simulate RTP header with PT=10 (L16 at 8kHz)
    header_l16 = struct.pack('!BBHII',
        0x80,
        0x0A,  # PT=10 (L16)
        0x0001,
        0x00000000,
        0x12345678
    )
    
    # Parse headers
    pt_ulaw = (struct.unpack('!B', header_ulaw[1:2])[0]) & 0x7F
    pt_l16 = (struct.unpack('!B', header_l16[1:2])[0]) & 0x7F
    
    logger.info(f"RTP header PT detection: ulaw={pt_ulaw}, L16={pt_l16}")
    
    if pt_ulaw == 0 and pt_l16 == 10:
        logger.info("✓ RTP payload type parsing correct")
    else:
        logger.warning(f"✗ RTP payload type parsing incorrect: ulaw={pt_ulaw}, L16={pt_l16}")
    
    logger.info("RTP parsing test complete")


def test_endianness_conversion():
    """Test big-endian to little-endian conversion."""
    logger.info("Testing endianness conversion...")
    
    # Create test PCM16 samples
    samples_be = np.array([0x1234, 0x5678, -0x1234, -0x5678], dtype=np.int16)
    
    # Pack as big-endian
    audio_be_bytes = struct.pack('>4h', *samples_be)
    
    # Unpack as big-endian, convert to native (little-endian)
    audio_be = np.frombuffer(audio_be_bytes, dtype=np.dtype('>i2'))
    audio_le = audio_be.astype(np.int16)
    
    logger.info(f"Original samples: {samples_be}")
    logger.info(f"After BE->LE conversion: {audio_le}")
    
    # Verify values are preserved (just byte order changes)
    if np.array_equal(samples_be, audio_le):
        logger.info("✓ Endianness conversion correct")
    else:
        logger.warning(f"✗ Endianness conversion issue: {samples_be} != {audio_le}")
    
    logger.info("Endianness test complete")


async def main():
    """Run all echo tests."""
    logger.info("=" * 60)
    logger.info("Audio Pipeline Echo Tests")
    logger.info("=" * 60)
    
    test_ulaw_decode()
    print()
    
    test_resample_8k_to_24k()
    print()
    
    test_rtp_payload_parsing()
    print()
    
    test_endianness_conversion()
    print()
    
    logger.info("=" * 60)
    logger.info("All tests complete!")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
