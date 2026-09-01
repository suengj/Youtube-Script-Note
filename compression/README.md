# 🎵 오디오 파일 자동 압축 도구

용량이 큰 오디오 파일을 다양한 품질로 자동 압축하는 파이썬 도구입니다.

**M4A 무손실 아카이브 (zip_process)**: 로컬에 받은 M4A를 **아카이브용 경로(예: 별도 SSD)**로 옮긴 뒤, 재인코딩 없이 zstd/7z로 무손실 압축·복원하려면 프로젝트 루트의 `zip_process.py`와 `config.json`의 `COMPRESSION_*` 설정을 사용하세요. AAC는 이미 압축된 포맷이므로 추가 용량 절감은 5~15% 수준일 수 있으나, 무결성(SHA256) 검증과 100% 복원이 가능합니다. 자세한 사용법은 아래 "M4A 무손실 아카이브 (zip_process)" 섹션을 참조하세요.

## 📋 주요 기능

- **다양한 압축 포맷**: MP3, AAC, OGG, FLAC 지원
- **품질 프리셋**: 4단계 압축 품질 설정
- **배치 처리**: 폴더 전체 오디오 파일 일괄 압축
- **사용자 정의**: 비트레이트 직접 설정 가능
- **상세 정보**: 압축 전후 파일 정보 비교

## 🚀 설치 및 설정

### 1. FFmpeg 설치 (필수)

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg

# Windows
# https://ffmpeg.org/download.html 에서 다운로드
```

### 2. 파이썬 의존성 설치

```bash
pip install -r requirements.txt
```

## 📖 사용법

### 기본 사용법

```bash
# 1. 먼저 음질 분석 (권장!)
python audio_compressor.py input.wav --analyze

# 2. 분석 결과를 보고 압축
python audio_compressor.py input.wav -o output.mp3 -p standard

# 3. 배치 압축 (폴더 전체)
python audio_compressor.py input_folder -o output_folder --batch

# 4. 특정 포맷과 품질로 압축
python audio_compressor.py input.wav -o output.mp3 -f mp3 -p standard

# 5. STT(음성인식) 최적화 압축
python audio_compressor.py input.wav -o output.wav --stt -f wav

# 6. 사용자 정의 비트레이트
python audio_compressor.py input.wav -o output.mp3 -f mp3 -b 128k
```

### 명령어 옵션

| 옵션 | 설명 | 기본값 |
|------|------|--------|
| `input` | 입력 파일/폴더 경로 | 필수 |
| `-o, --output` | 출력 파일/폴더 경로 | 자동 생성 |
| `-f, --format` | 출력 포맷 (mp3, aac, ogg, flac) | mp3 |
| `-p, --preset` | 압축 프리셋 | standard |
| `-b, --bitrate` | 사용자 정의 비트레이트 | 프리셋 사용 |
| `--batch` | 배치 처리 모드 | False |
| `--analyze` | 음질 분석만 수행 (압축하지 않음) | False |
| `--stt` | STT(음성인식) 최적화 압축 | False |
| `--presets` | 사용 가능한 프리셋 표시 | - |
| `--ffmpeg-path` | FFmpeg 경로 | /opt/homebrew/bin/ffmpeg |

## 🎚️ 압축 품질 설정

### 프리셋별 설정

| 프리셋 | MP3 | AAC | OGG | 설명 |
|--------|-----|-----|-----|------|
| `high_quality` | 320k | 256k | 320k | 최고 품질 |
| `standard` | 192k | 128k | 192k | 표준 품질 (권장) |
| `compressed` | 128k | 96k | 128k | 압축 품질 |
| `low_size` | 64k | 64k | 64k | 최소 용량 |

### 비트레이트별 품질 가이드

- **320kbps**: CD 품질, 거의 원음과 구분 어려움
- **256kbps**: 매우 높은 품질
- **192kbps**: 높은 품질 (일반적으로 권장)
- **128kbps**: 표준 품질 (스트리밍용)
- **96kbps**: 보통 품질
- **64kbps**: 낮은 품질 (음성/팟캐스트용)

## 💡 사용 예제

### 1. 음질 분석 (권장!)

```bash
# 먼저 파일의 음질을 분석하고 압축 권장사항 확인
python audio_compressor.py music.wav --analyze

# 분석 결과 예시:
# 🎵 오디오 파일 음질 분석 결과
# ============================================================
# 📄 파일 정보:
#    크기: 25.3 MB
#    길이: 180.5 초
#    코덱: M4A
# 🎚️ 음질 등급: Very Good (점수: 7.8/10)
# 📊 상세 평가:
#    비트레이트: 256kbps (Very Good)
#    샘플레이트: 44100Hz (Very Good)
#    채널: 2 (Stereo)
#    코덱 품질: 8/10
# 💡 압축 권장사항:
#    압축 권장: ✅ 예
#    이유: 고품질 원본이지만 용량 최적화 가능
# 🎯 권장 설정:
#    권장 프리셋: high_quality, standard
#    권장 비트레이트: 256k
# 💰 예상 용량 절약:
#    high_quality: 320k → 19.8MB (21.7% 절약)
#    standard: 192k → 11.9MB (53.0% 절약)
```

### 2. 분석 결과를 바탕으로 압축

```bash
# 분석에서 권장한 설정으로 압축
python audio_compressor.py music.wav -o music.mp3 -f mp3 -p standard

# 사용자 정의 비트레이트로 압축
python audio_compressor.py music.wav -o music.mp3 -f mp3 -b 192k
```

### 3. 팟캐스트/음성 파일 압축

```bash
# 먼저 분석
python audio_compressor.py podcast.wav --analyze

# 분석 결과에 따라 압축
python audio_compressor.py podcast.wav -o podcast.mp3 -f mp3 -p compressed
```

### 4. 배치 처리

```bash
# 폴더 전체 MP3로 압축
python audio_compressor.py audio_files/ -o compressed/ --batch -f mp3 -p standard
```

### 5. STT(음성인식) 최적화 압축

```bash
# STT 최적화 (WAV, 16kHz, 모노)
python audio_compressor.py input.wav -o output.wav --stt -f wav

# STT 최적화 (MP3, 192kbps, 16kHz, 모노)
python audio_compressor.py input.wav -o output.mp3 --stt -f mp3

# STT 최적화 (AAC, 128kbps, 16kHz, 모노)
python audio_compressor.py input.wav -o output.aac --stt -f aac
```

### 6. 사용자 정의 비트레이트

```bash
# 특정 비트레이트로 압축
python audio_compressor.py input.wav -o output.mp3 -f mp3 -b 256k

# AAC 포맷으로 압축
python audio_compressor.py input.wav -o output.aac -f aac -b 128k
```

## 🔧 프로그래밍 방식 사용

```python
from audio_compressor import AudioCompressor

# 압축기 초기화
compressor = AudioCompressor()

# 1. 음질 분석 (권장!)
analysis_result = compressor.analyze_and_recommend("input.wav")
# 또는
analysis_result = compressor.analyze_audio_quality("input.wav")
compressor.print_quality_analysis(analysis_result)

# 2. 분석 결과를 바탕으로 압축
if analysis_result["recommendations"]["should_compress"]:
    recommended_preset = analysis_result["recommendations"]["recommended_presets"][0]
    success, message = compressor.compress_audio(
        "input.wav", 
        "output.mp3", 
        "mp3", 
        recommended_preset
    )

# 3. 단일 파일 압축 (분석 없이)
success, message = compressor.compress_audio(
    "input.wav", 
    "output.mp3", 
    "mp3", 
    "standard"
)

# 4. 배치 압축
results = compressor.batch_compress(
    input_dir="input_folder",
    output_dir="output_folder",
    formats=["mp3", "aac"],
    preset="standard"
)

# 5. 파일 정보 확인
info = compressor.get_file_info("audio.wav")
print(f"파일 크기: {info['file_size_mb']}MB")
```

## 🎤 STT(음성인식) 최적화 가이드

### STT 성능에 영향을 주는 요소들

| 요소 | 최적 설정 | 이유 |
|------|-----------|------|
| **샘플레이트** | 16kHz | Whisper 등 대부분 STT 모델의 표준 |
| **채널** | 모노 (1채널) | 스테레오는 STT 성능에 도움 안됨 |
| **비트레이트** | 128-192kbps | 64kbps 이하는 정확도 급격히 하락 |
| **포맷** | WAV > MP3 > AAC | 무손실이 가장 좋지만 용량 고려 |

### 비트레이트별 STT 정확도

| 비트레이트 | STT 정확도 | 용도 |
|------------|------------|------|
| 320kbps | 95-98% | 최고 정확도 필요 |
| 192kbps | 90-95% | **권장** (품질/용량 균형) |
| 128kbps | 85-90% | 일반적인 용도 |
| 64kbps | 70-80% | 음성만 있는 경우 |
| 32kbps 이하 | 50-70% | **STT 비권장** |

### STT 최적화 권장사항

1. **음성인식용**: `--stt -f wav` (무손실, 16kHz, 모노)
2. **용량 고려**: `--stt -f mp3` (192kbps, 16kHz, 모노)
3. **최소 용량**: `--stt -f aac` (128kbps, 16kHz, 모노)

## 📊 압축 효과 예시

| 원본 | 압축 후 | 압축률 | 품질 | STT 정확도 |
|------|---------|--------|------|------------|
| WAV 50MB | MP3 4MB (320k) | 92% | 최고 | 95-98% |
| WAV 50MB | MP3 2.5MB (192k) | 95% | 높음 | 90-95% |
| WAV 50MB | MP3 1.6MB (128k) | 97% | 표준 | 85-90% |
| WAV 50MB | MP3 0.8MB (64k) | 98% | 낮음 | 70-80% |
| WAV 50MB | STT WAV 16kHz | 70% | STT 최적 | 95-98% |

## ⚠️ 주의사항

1. **FFmpeg 필수**: 압축을 위해 FFmpeg가 설치되어 있어야 합니다
2. **원본 백업**: 압축 전 원본 파일을 백업하세요
3. **품질 테스트**: 용도에 맞는 품질을 미리 테스트해보세요
4. **저작권**: 저작권이 있는 음악의 압축 시 주의하세요

## 🐛 문제 해결

### FFmpeg를 찾을 수 없습니다
```bash
# FFmpeg 경로 확인
which ffmpeg

# 경로 지정하여 실행
python audio_compressor.py input.wav -o output.mp3 --ffmpeg-path /usr/local/bin/ffmpeg
```

### 압축 실패
- 파일이 손상되지 않았는지 확인
- 충분한 디스크 공간이 있는지 확인
- 파일 권한 확인

### 품질이 너무 낮다
- 더 높은 비트레이트 사용 (`-b 256k`)
- `high_quality` 프리셋 사용 (`-p high_quality`)

## 📈 성능 최적화

- **배치 처리**: 여러 파일을 한 번에 처리
- **SSD 사용**: 빠른 디스크에서 작업
- **충분한 메모리**: 대용량 파일 처리 시

## 🔄 업데이트 이력

- v1.0.0: 초기 버전 (기본 압축 기능)
- v1.1.0: 배치 처리 추가
- v1.2.0: 사용자 정의 비트레이트 지원
- v1.3.0: 파일 정보 분석 기능 추가

## M4A 무손실 아카이브 (zip_process)

로컬에 다운로드한 M4A(128kbps AAC)를 **아카이브용 경로(별도 SSD 등)**에 두고, **재인코딩 없이** zstd 또는 7z로 무손실 압축·복원하는 스크립트입니다. 프로젝트 루트의 `zip_process.py`와 `config.json`을 사용합니다.

### 특징

- **무손실**: 원본 바이너리를 그대로 압축하므로 복원 시 100% 동일.
- **무결성**: 압축 전 SHA256 저장, 복원 후 검증.
- **메타데이터**: checksum, 크기, mime-type, sample_rate, channels, duration 등을 `.meta.json`에 저장.
- **상태 관리**: `compression_state.json`으로 압축된 파일 목록 관리.

### 주의

- AAC/M4A는 이미 손실 압축되어 있어, zstd/7z 추가 압축률은 **보통 5~15%** 수준입니다. 용량보다 무결성·복원성이 목적일 때 유용합니다.
- 압축(zip) 시 검증에 성공한 경우에만 원본 M4A를 삭제합니다.

### 설정 (config.json)

| 키 | 설명 | 기본값 |
|----|------|--------|
| `COMPRESSION_AUDIO_PATH` | 아카이브 루트 경로 (M4A가 있는 디렉터리, 예: SSD 경로) | `""` |
| `COMPRESSION_MODE` | `zip` 또는 `unzip` | `zip` |
| `COMPRESSION_METHOD` | `zstd` 또는 `7z` | `zstd` |
| `ZSTD_LEVEL` | zstd 압축 레벨 (1~22, 19=ultra) | `19` |
| `COMPRESSION_STATE_FILE` | 상태 JSON 파일명 | `compression_state.json` |
| `COMPRESSION_RECURSIVE` | 하위 디렉터리까지 스캔 | `false` |
| `COMPRESSION_MIN_SIZE_MB` | 이 값(MB) 미만 M4A는 압축 생략 (작은 파일은 zstd 후 커질 수 있음). 0이면 제한 없음 | `10` |
| `COMPRESSION_DELETE_AFTER_UNZIP` | unzip 후 복원 성공 시 .zst(또는 .7z) 및 .meta.json 삭제 여부 | `true` |

### 실행

1. `config.json`에 `COMPRESSION_AUDIO_PATH`를 아카이브용 경로로 설정.
2. `COMPRESSION_MODE`를 `zip`(압축) 또는 `unzip`(복원)으로 설정.
3. 프로젝트 루트에서 실행:

```bash
python zip_process.py
```

### 필요 도구

- **zstd**: `brew install zstd` (macOS)
- **ffprobe**: FFmpeg에 포함 (`brew install ffmpeg`)
- **7z** (선택, COMPRESSION_METHOD=7z일 때): p7zip 등 설치

---

## 📞 지원

문제가 있거나 기능 요청이 있으시면 이슈를 등록해주세요.

---

**💡 팁**: 음질과 용량의 균형을 위해 `standard` 프리셋(192kbps)을 먼저 시도해보세요!
