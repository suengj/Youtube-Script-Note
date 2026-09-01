#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
오디오 압축 사용 예제 및 테스트
"""

from audio_compressor import AudioCompressor
import os
from pathlib import Path

def example_audio_analysis():
    """오디오 파일 음질 분석 예제"""
    print("🎵 오디오 파일 음질 분석 예제")
    print("=" * 40)
    
    compressor = AudioCompressor()
    
    # 예제 파일 경로 (실제 파일로 변경 필요)
    input_file = "sample_audio.m4a"  # 실제 파일 경로로 변경
    
    if os.path.exists(input_file):
        # 음질 분석 수행
        analysis_result = compressor.analyze_and_recommend(input_file)
        
        # 분석 결과를 기반으로 압축 권장사항 확인
        if analysis_result and "recommendations" in analysis_result:
            recommendations = analysis_result["recommendations"]
            
            if recommendations["should_compress"]:
                print(f"\n💡 권장 압축 설정:")
                print(f"   권장 프리셋: {', '.join(recommendations['recommended_presets'])}")
                print(f"   권장 비트레이트: {recommendations['custom_bitrate']}")
                
                # 권장 설정으로 압축 테스트
                for preset in recommendations["recommended_presets"][:2]:  # 상위 2개만 테스트
                    output_filename = f"recommended_{preset}.mp3"
                    print(f"\n🔄 권장 설정 '{preset}'으로 압축 중...")
                    
                    success, message = compressor.compress_audio(
                        input_file, 
                        output_filename, 
                        "mp3", 
                        preset
                    )
                    
                    if success:
                        compressed_info = compressor.get_file_info(output_filename)
                        print(f"   ✅ {message}")
                        print(f"   압축된 크기: {compressed_info.get('file_size_mb', 0):.1f}MB")
                    else:
                        print(f"   ❌ {message}")
            else:
                print(f"\n⚠️ 압축 권장하지 않음: {recommendations['reason']}")
    else:
        print(f"⚠️ 예제 파일을 찾을 수 없습니다: {input_file}")
        print("실제 오디오 파일 경로로 변경해주세요.")

def example_single_file_compression():
    """단일 파일 압축 예제 (분석 없이)"""
    print("🎵 단일 파일 압축 예제 (분석 없이)")
    print("=" * 40)
    
    compressor = AudioCompressor()
    
    # 예제 파일 경로 (실제 파일로 변경 필요)
    input_file = "sample_audio.m4a"  # 실제 파일 경로로 변경
    output_file = "compressed_audio.mp3"
    
    if os.path.exists(input_file):
        # 원본 파일 정보
        print(f"📄 원본 파일: {input_file}")
        original_info = compressor.get_file_info(input_file)
        print(f"   크기: {original_info.get('file_size_mb', 0):.1f}MB")
        print(f"   코덱: {original_info.get('codec', 'unknown')}")
        print(f"   비트레이트: {original_info.get('bitrate', 0)} bps")
        
        # 다양한 품질로 압축 테스트
        presets = ["high_quality", "standard", "compressed", "low_size"]
        
        for preset in presets:
            output_filename = f"compressed_{preset}.mp3"
            print(f"\n🔄 {preset} 압축 중...")
            
            success, message = compressor.compress_audio(
                input_file, 
                output_filename, 
                "mp3", 
                preset
            )
            
            if success:
                compressed_info = compressor.get_file_info(output_filename)
                print(f"   ✅ {message}")
                print(f"   압축된 크기: {compressed_info.get('file_size_mb', 0):.1f}MB")
            else:
                print(f"   ❌ {message}")
    else:
        print(f"⚠️ 예제 파일을 찾을 수 없습니다: {input_file}")
        print("실제 오디오 파일 경로로 변경해주세요.")

def example_batch_compression():
    """배치 압축 예제"""
    print("\n🎵 배치 압축 예제")
    print("=" * 40)
    
    compressor = AudioCompressor()
    
    # 입력/출력 디렉토리
    input_dir = "input_audio"  # 실제 디렉토리로 변경
    output_dir = "compressed_audio"
    
    if os.path.exists(input_dir):
        # 배치 압축 실행
        results = compressor.batch_compress(
            input_dir=input_dir,
            output_dir=output_dir,
            formats=["mp3", "aac"],  # 여러 포맷으로 압축
            preset="standard",
            file_extensions=[".mp3", ".m4a", ".wav", ".flac", ".aac"]
        )
        
        print(f"📊 배치 처리 결과:")
        print(f"   총 파일: {results['total_files']}")
        print(f"   성공: {results['successful']}")
        print(f"   실패: {results['failed']}")
        
        # 실패한 파일들 표시
        failed_files = [detail for detail in results['details'] if not detail['success']]
        if failed_files:
            print(f"\n❌ 실패한 파일들:")
            for detail in failed_files:
                print(f"   {detail['input_file']}: {detail['message']}")
    else:
        print(f"⚠️ 입력 디렉토리를 찾을 수 없습니다: {input_dir}")
        print("실제 오디오 파일들이 있는 디렉토리 경로로 변경해주세요.")

def example_stt_optimization():
    """STT 최적화 압축 예제"""
    print("\n🎤 STT(음성인식) 최적화 압축 예제")
    print("=" * 40)
    
    compressor = AudioCompressor()
    
    input_file = "sample_audio.m4a"  # 실제 파일로 변경
    
    if os.path.exists(input_file):
        # 먼저 음질 분석
        print("🔍 음질 분석 중...")
        analysis_result = compressor.analyze_and_recommend(input_file)
        
        # STT 최적화 압축
        print(f"\n🎤 STT 최적화 압축 중...")
        
        # 다양한 STT 최적화 포맷으로 테스트
        stt_formats = ["wav", "mp3", "aac"]
        
        for format_type in stt_formats:
            output_file = f"stt_optimized.{format_type}"
            print(f"\n🔄 {format_type.upper()} 포맷으로 STT 최적화 중...")
            
            success, message = compressor.compress_for_stt(
                input_file, 
                output_file, 
                format_type
            )
            
            if success:
                compressed_info = compressor.get_file_info(output_file)
                print(f"   ✅ {message}")
                print(f"   압축된 크기: {compressed_info.get('file_size_mb', 0):.1f}MB")
                print(f"   샘플레이트: {compressed_info.get('sample_rate', 0)}Hz")
                print(f"   채널: {compressed_info.get('channels', 0)}")
            else:
                print(f"   ❌ {message}")
    else:
        print(f"⚠️ 예제 파일을 찾을 수 없습니다: {input_file}")

def example_custom_compression():
    """사용자 정의 압축 예제"""
    print("\n🎵 사용자 정의 압축 예제")
    print("=" * 40)
    
    compressor = AudioCompressor()
    
    input_file = "sample_audio.m4a"  # 실제 파일로 변경
    
    if os.path.exists(input_file):
        # 다양한 사용자 정의 비트레이트로 테스트
        custom_bitrates = ["64k", "128k", "192k", "256k", "320k"]
        
        for bitrate in custom_bitrates:
            output_file = f"custom_{bitrate}.mp3"
            print(f"\n🔄 {bitrate} 비트레이트로 압축 중...")
            
            success, message = compressor.compress_audio(
                input_file, 
                output_file, 
                "mp3", 
                custom_bitrate=bitrate
            )
            
            if success:
                compressed_info = compressor.get_file_info(output_file)
                print(f"   ✅ {message}")
                print(f"   압축된 크기: {compressed_info.get('file_size_mb', 0):.1f}MB")
            else:
                print(f"   ❌ {message}")
    else:
        print(f"⚠️ 예제 파일을 찾을 수 없습니다: {input_file}")

def show_compression_guide():
    """압축 가이드 표시"""
    print("\n📚 오디오 압축 가이드")
    print("=" * 50)
    
    print("""
🎵 압축 방식별 특징:

1. 무손실 압축 (FLAC, ALAC)
   ✅ 음질 손실 없음
   ✅ 용량 50-70% 감소
   ❌ 파일 크기가 여전히 큼
   💡 용도: 아카이브, 고품질 보관

2. 손실 압축 (MP3, AAC, OGG)
   ✅ 용량 대폭 감소 (90% 이상)
   ❌ 음질 손실 있음
   💡 용도: 스트리밍, 일반 재생

🎚️ 비트레이트별 품질:

• 320kbps: 거의 원음과 구분 어려움 (CD 품질)
• 256kbps: 매우 높은 품질
• 192kbps: 높은 품질 (권장)
• 128kbps: 표준 품질 (일반적)
• 96kbps: 보통 품질
• 64kbps: 낮은 품질 (음성용)

🎯 용도별 권장 설정:

• 음악 아카이브: FLAC 또는 MP3 320kbps
• 일반 음악 재생: MP3 192kbps
• 팟캐스트/음성: MP3 128kbps
• 스트리밍: AAC 128kbps
• 최소 용량: MP3 64kbps

⚙️ 프리셋 설명:

• high_quality: 최고 품질 (320kbps)
• standard: 표준 품질 (192kbps) - 권장
• compressed: 압축 품질 (128kbps)
• low_size: 최소 용량 (64kbps)
""")

def create_test_directory():
    """테스트용 디렉토리 구조 생성"""
    print("\n📁 테스트 디렉토리 생성")
    print("=" * 30)
    
    # 테스트 디렉토리 생성
    test_dirs = ["input_audio", "compressed_audio", "test_results"]
    
    for dir_name in test_dirs:
        os.makedirs(dir_name, exist_ok=True)
        print(f"✅ {dir_name}/ 디렉토리 생성")
    
    # README 파일 생성
    readme_content = """# 오디오 압축 테스트 디렉토리

## 사용법

1. `input_audio/` 폴더에 압축할 오디오 파일들을 넣으세요
2. `python audio_compression_examples.py` 실행
3. `compressed_audio/` 폴더에서 압축된 파일들을 확인하세요

## 지원 포맷

- 입력: MP3, M4A, WAV, FLAC, AAC
- 출력: MP3, AAC, OGG, FLAC

## 명령어 예제

```bash
# 단일 파일 압축
python audio_compressor.py input.wav -o output.mp3 -f mp3 -p standard

# 배치 압축
python audio_compressor.py input_folder -o output_folder --batch -f mp3 -p standard

# 사용자 정의 비트레이트
python audio_compressor.py input.wav -o output.mp3 -f mp3 -b 128k
```
"""
    
    with open("README.md", "w", encoding="utf-8") as f:
        f.write(readme_content)
    
    print("✅ README.md 파일 생성")

def main():
    """메인 실행 함수"""
    print("🎵 오디오 압축 예제 및 테스트")
    print("=" * 50)
    
    # 압축 가이드 표시
    show_compression_guide()
    
    # 테스트 디렉토리 생성
    create_test_directory()
    
    # 예제 실행 (실제 파일이 있을 때만)
    print("\n" + "="*50)
    print("📝 예제 실행을 위해서는 실제 오디오 파일이 필요합니다.")
    print("   input_audio/ 폴더에 테스트할 오디오 파일을 넣고 다시 실행하세요.")
    
    # 예제 함수들 (주석 해제하여 사용)
    # example_audio_analysis()  # 음질 분석 예제 (권장)
    # example_stt_optimization()  # STT 최적화 예제 (음성인식용)
    # example_single_file_compression()  # 단순 압축 예제
    # example_batch_compression()
    # example_custom_compression()

if __name__ == "__main__":
    main()
