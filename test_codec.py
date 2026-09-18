"""Test cepat: encode PCM sine wave -> opus_stream, verifikasi format."""
import math
import struct
import sys
import wave
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from opus2gh.opus_codec import pcm_to_opus_stream, FFMPEG, OPUS_SR, FRAME_SAMPLES

# 1. Buat PCM s16le 16kHz mono 3 detik (sine 440Hz + fade)
dur_s = 3
n = OPUS_SR * dur_s
pcm = bytearray()
for i in range(n):
    v = int(12000 * math.sin(2 * math.pi * 440 * i / OPUS_SR))
    pcm += struct.pack('<h', v)

test_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'music', '.tmp')
os.makedirs(test_dir, exist_ok=True)
pcm_file = os.path.join(test_dir, 'test.pcm')
opus_file = os.path.join(test_dir, 'test.opus_stream')

with open(pcm_file, 'wb') as f:
    f.write(pcm)

print(f"PCM test: {len(pcm)} bytes ({dur_s}s)")
print(f"ffmpeg: {FFMPEG}")

# 2. Encode
ok = pcm_to_opus_stream(pcm_file, opus_file)
print(f"encode ok: {ok}")
assert ok, "ENCODE GAGAL"

# 3. Verifikasi format: [uint16_be len][packet]...
size = os.path.getsize(opus_file)
print(f"opus_stream: {size} bytes ({size/1024:.1f} KB)")

with open(opus_file, 'rb') as f:
    n_packets = 0
    total_payload = 0
    while True:
        hdr = f.read(2)
        if len(hdr) < 2:
            break
        plen = struct.unpack('>H', hdr)[0]
        pkt = f.read(plen)
        if len(pkt) != plen:
            print("FORMAT RUSAK: packet terpotong")
            sys.exit(1)
        n_packets += 1
        total_payload += plen

expected_packets = dur_s * 1000 // 60  # 60ms frames
print(f"packets: {n_packets} (expected ~{expected_packets})")
print(f"payload: {total_payload} bytes, overhead hdr: {n_packets*2} bytes")
bitrate_kbps = (total_payload * 8) / dur_s / 1000
print(f"effective bitrate: {bitrate_kbps:.1f} kbps (target 16)")

assert n_packets >= expected_packets - 2, "jumlah packet salah"
assert bitrate_kbps <= 20, "bitrate melebihi target"

# 4. Test decode balik via opuslib (pastikan packet valid)
try:
    import opuslib
    dec = opuslib.Decoder(OPUS_SR, 1)
    with open(opus_file, 'rb') as f:
        decoded_frames = 0
        while True:
            hdr = f.read(2)
            if len(hdr) < 2:
                break
            plen = struct.unpack('>H', hdr)[0]
            pkt = f.read(plen)
            dec.decode(pkt, FRAME_SAMPLES)
            decoded_frames += 1
    print(f"decode balik OK: {decoded_frames} frames")
except ImportError:
    print("opuslib tidak ada, skip decode test")

# cleanup
os.remove(pcm_file)
os.remove(opus_file)
print("\n✅ SEMUA TEST PASS")
