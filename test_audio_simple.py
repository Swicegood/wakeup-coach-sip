"""Simple audio test - generate a 440Hz tone and send via RTP."""

import asyncio
import math
import struct
import logging
from rtp_server import RTPServer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test-audio")


async def main():
    # Create RTP server
    rtp_server = RTPServer(logger, port=0)
    await rtp_server.start()

    logger.info(f"RTP server started on port {rtp_server.port}")
    logger.info(f"To test: create external media to this host on port {rtp_server.port}")
    logger.info("Send me the Asterisk RTP host:port when ready...")

    # Wait for user to provide Asterisk RTP address
    asterisk_host = input("Enter Asterisk RTP host (e.g., 10.0.10.6): ").strip()
    asterisk_port = int(input("Enter Asterisk RTP port: ").strip())

    logger.info(f"Will send audio to {asterisk_host}:{asterisk_port}")
    logger.info("Generating 440Hz tone for 5 seconds...")

    # Generate 440Hz sine wave tone at 16kHz sample rate
    sample_rate = 16000
    frequency = 440  # A4 note
    amplitude = 8000  # Moderate volume
    duration = 5  # seconds

    samples_per_packet = 160  # 10ms at 16kHz
    total_packets = (sample_rate * duration) // samples_per_packet

    for packet_num in range(total_packets):
        # Generate samples for this packet
        samples = []
        for i in range(samples_per_packet):
            sample_index = packet_num * samples_per_packet + i
            t = sample_index / sample_rate
            value = int(amplitude * math.sin(2 * math.pi * frequency * t))
            samples.append(value)

        # Convert to little-endian bytes (will be converted to big-endian in send_audio)
        audio_data = struct.pack(f'<{len(samples)}h', *samples)

        # Send via RTP
        await rtp_server.send_audio(audio_data, asterisk_host, asterisk_port)

        if packet_num % 50 == 0:
            logger.info(f"Sent packet {packet_num}/{total_packets}")

        # Wait 10ms between packets
        await asyncio.sleep(0.01)

    logger.info("Tone generation complete!")
    await rtp_server.stop()


if __name__ == "__main__":
    asyncio.run(main())
