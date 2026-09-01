#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
오디오 파일 자동 압축 도구
다양한 압축 옵션과 품질 설정을 제공합니다.
"""

import os
import subprocess
import argparse
from pathlib import Path
from typing import List, Tuple, Optional
import json
from datetime import datetime

class AudioCompressor:
    def __init__(self, ffmpeg_path: str = "/opt/homebrew/bin/ffmpeg"):
        """
        오디오 압축기 초기화
        
        Args:
            ffmpeg_path: FFmpeg 실행 파일 경로
        """
        self.ffmpeg_path = ffmpeg_path
        self.compression_presets = {
            "high_quality": {
                "mp3": {"bitrate": "320k", "description": "최고 품질 (320kbps)"},
                "aac": {"bitrate": "256k", "description": "최고 품질 (256kbps)"},
                "ogg": {"bitrate": "320k", "description": "최고 품질 (320kbps)"}
            },
            "standard": {
                "mp3": {"bitrate": "192k", "description": "표준 품질 (192kbps)"},
                "aac": {"bitrate": "128k", "description": "표준 품질 (128kbps)"},
                "ogg": {"bitrate": "192k", "description": "표준 품질 (192kbps)"}
            },
            "compressed": {
                "mp3": {"bitrate": "128k", "description": "압축 품질 (128kbps)"},
                "aac": {"bitrate": "96k", "description": "압축 품질 (96kbps)"},
                "ogg": {"bitrate": "128k", "description": "압축 품질 (128kbps)"}
            },
            "low_size": {
                "mp3": {"bitrate": "64k", "description": "최소 용량 (64kbps)"},
                "aac": {"bitrate": "64k", "description": "최소 용량 (64kbps)"},
                "ogg": {"bitrate": "64k", "description": "최소 용량 (64kbps)"}
            },
            "stt_optimized": {
                "mp3": {"bitrate": "192k", "description": "STT 최적화 (192kbps, 16kHz)"},
                "aac": {"bitrate": "128k", "description": "STT 최적화 (128kbps, 16kHz)"},
                "wav": {"bitrate": "256k", "description": "STT 최적화 (WAV, 16kHz)"}
            }
        }
    
    def check_ffmpeg(self) -> bool:
        """FFmpeg 설치 확인"""
        try:
            result = subprocess.run([self.ffmpeg_path, "-version"], 
                                  capture_output=True, text=True, check=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            print(f"❌ FFmpeg를 찾을 수 없습니다: {self.ffmpeg_path}")
            print("FFmpeg 설치 방법:")
            print("  macOS: brew install ffmpeg")
            print("  Ubuntu: sudo apt install ffmpeg")
            print("  Windows: https://ffmpeg.org/download.html")
            return False
    
    def get_file_info(self, file_path: str) -> dict:
        """오디오 파일 정보 추출"""
        try:
            cmd = [
                self.ffmpeg_path, "-i", file_path,
                "-f", "null", "-", "-v", "quiet", "-stats"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, stderr=subprocess.STDOUT)
            
            # 파일 크기
            file_size = os.path.getsize(file_path)
            
            # FFprobe로 상세 정보 추출
            probe_cmd = [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", file_path
            ]
            probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
            
            if probe_result.returncode == 0:
                info = json.loads(probe_result.stdout)
                audio_stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
                
                return {
                    "file_size_mb": round(file_size / (1024 * 1024), 2),
                    "duration": float(info.get("format", {}).get("duration", 0)),
                    "bitrate": int(audio_stream.get("bit_rate", 0)),
                    "sample_rate": int(audio_stream.get("sample_rate", 0)),
                    "channels": int(audio_stream.get("channels", 0)),
                    "codec": audio_stream.get("codec_name", "unknown")
                }
            else:
                return {"file_size_mb": round(file_size / (1024 * 1024), 2)}
                
        except Exception as e:
            print(f"⚠️ 파일 정보 추출 실패: {e}")
            return {"file_size_mb": round(os.path.getsize(file_path) / (1024 * 1024), 2)}
    
    def analyze_audio_quality(self, file_path: str) -> dict:
        """오디오 파일의 음질을 분석하고 압축 권장사항을 제공"""
        try:
            info = self.get_file_info(file_path)
            
            # 기본 정보
            file_size_mb = info.get("file_size_mb", 0)
            duration = info.get("duration", 0)
            bitrate = info.get("bitrate", 0)
            sample_rate = info.get("sample_rate", 0)
            channels = info.get("channels", 0)
            codec = info.get("codec", "unknown")
            
            # 음질 등급 분석
            quality_grade = self._evaluate_quality_grade(bitrate, sample_rate, channels, codec)
            
            # 압축 권장사항 생성
            recommendations = self._generate_compression_recommendations(
                quality_grade, bitrate, file_size_mb, duration, codec
            )
            
            # 스펙트럼 분석 (고급)
            spectrum_info = self._analyze_spectrum(file_path)
            
            return {
                "basic_info": info,
                "quality_grade": quality_grade,
                "recommendations": recommendations,
                "spectrum_analysis": spectrum_info,
                "analysis_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
        except Exception as e:
            print(f"⚠️ 음질 분석 실패: {e}")
            return {"error": str(e)}
    
    def _evaluate_quality_grade(self, bitrate: int, sample_rate: int, channels: int, codec: str) -> dict:
        """음질 등급 평가"""
        grade = "Unknown"
        score = 0
        details = []
        
        # 비트레이트 평가
        if bitrate >= 320000:
            bitrate_grade = "Excellent"
            bitrate_score = 10
        elif bitrate >= 256000:
            bitrate_grade = "Very Good"
            bitrate_score = 8
        elif bitrate >= 192000:
            bitrate_grade = "Good"
            bitrate_score = 6
        elif bitrate >= 128000:
            bitrate_grade = "Fair"
            bitrate_score = 4
        elif bitrate >= 64000:
            bitrate_grade = "Poor"
            bitrate_score = 2
        else:
            bitrate_grade = "Very Poor"
            bitrate_score = 1
        
        # 샘플레이트 평가
        if sample_rate >= 48000:
            sample_grade = "Excellent"
            sample_score = 10
        elif sample_rate >= 44100:
            sample_grade = "Very Good"
            sample_score = 8
        elif sample_rate >= 22050:
            sample_grade = "Fair"
            sample_score = 5
        else:
            sample_grade = "Poor"
            sample_score = 2
        
        # 채널 평가
        if channels >= 2:
            channel_grade = "Stereo"
            channel_score = 10
        else:
            channel_grade = "Mono"
            channel_score = 5
        
        # 코덱 평가
        codec_scores = {
            "flac": 10, "alac": 10, "wav": 10,  # 무손실
            "aac": 8, "mp3": 7, "ogg": 7,      # 손실 압축
            "m4a": 8, "opus": 8,               # 고급 손실 압축
        }
        codec_score = codec_scores.get(codec.lower(), 5)
        
        # 전체 점수 계산
        total_score = (bitrate_score * 0.4 + sample_score * 0.3 + 
                      channel_score * 0.1 + codec_score * 0.2)
        
        # 등급 결정
        if total_score >= 9:
            grade = "Excellent"
        elif total_score >= 7:
            grade = "Very Good"
        elif total_score >= 5:
            grade = "Good"
        elif total_score >= 3:
            grade = "Fair"
        else:
            grade = "Poor"
        
        return {
            "overall_grade": grade,
            "overall_score": round(total_score, 1),
            "bitrate": {"value": bitrate, "grade": bitrate_grade, "score": bitrate_score},
            "sample_rate": {"value": sample_rate, "grade": sample_grade, "score": sample_score},
            "channels": {"value": channels, "grade": channel_grade, "score": channel_score},
            "codec": {"value": codec, "score": codec_score}
        }
    
    def _generate_compression_recommendations(self, quality_grade: dict, bitrate: int, 
                                            file_size_mb: float, duration: float, codec: str) -> dict:
        """압축 권장사항 생성"""
        recommendations = {
            "should_compress": True,
            "reason": "",
            "recommended_presets": [],
            "avoid_presets": [],
            "custom_bitrate": None,
            "estimated_savings": {}
        }
        
        current_bitrate_kbps = bitrate // 1000
        overall_grade = quality_grade["overall_grade"]
        
        # 압축 필요성 판단
        if overall_grade == "Excellent" and current_bitrate_kbps >= 256:
            recommendations["should_compress"] = True
            recommendations["reason"] = "고품질 원본이지만 용량 최적화 가능"
        elif overall_grade in ["Very Good", "Good"]:
            recommendations["should_compress"] = True
            recommendations["reason"] = "적당한 품질로 압축 가능"
        elif overall_grade == "Fair":
            recommendations["should_compress"] = False
            recommendations["reason"] = "이미 압축된 파일로 추가 압축 시 품질 저하 우려"
        else:
            recommendations["should_compress"] = False
            recommendations["reason"] = "낮은 품질로 추가 압축 비권장"
        
        # 권장 프리셋
        if recommendations["should_compress"]:
            if current_bitrate_kbps >= 320:
                recommendations["recommended_presets"] = ["high_quality", "standard"]
                recommendations["custom_bitrate"] = "256k"
            elif current_bitrate_kbps >= 192:
                recommendations["recommended_presets"] = ["standard", "compressed"]
                recommendations["custom_bitrate"] = "128k"
            elif current_bitrate_kbps >= 128:
                recommendations["recommended_presets"] = ["compressed"]
                recommendations["custom_bitrate"] = "96k"
            else:
                recommendations["recommended_presets"] = ["low_size"]
                recommendations["custom_bitrate"] = "64k"
            
            # 피해야 할 프리셋
            if current_bitrate_kbps < 128:
                recommendations["avoid_presets"] = ["high_quality", "standard"]
        
        # 예상 용량 절약 계산
        if recommendations["should_compress"]:
            for preset in recommendations["recommended_presets"]:
                if preset in self.compression_presets:
                    target_bitrate = int(self.compression_presets[preset]["mp3"]["bitrate"].replace("k", ""))
                    estimated_size = file_size_mb * (target_bitrate / current_bitrate_kbps)
                    savings = ((file_size_mb - estimated_size) / file_size_mb) * 100
                    recommendations["estimated_savings"][preset] = {
                        "target_bitrate": f"{target_bitrate}k",
                        "estimated_size_mb": round(estimated_size, 1),
                        "savings_percent": round(savings, 1)
                    }
        
        return recommendations
    
    def _analyze_spectrum(self, file_path: str) -> dict:
        """스펙트럼 분석 (고급 기능)"""
        try:
            # FFmpeg로 주파수 분석
            cmd = [
                self.ffmpeg_path, "-i", file_path,
                "-af", "astats=metadata=1:reset=1",
                "-f", "null", "-", "-v", "quiet"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, stderr=subprocess.STDOUT)
            
            # 기본 스펙트럼 정보 (실제로는 더 복잡한 분석이 필요)
            return {
                "analysis_available": True,
                "note": "고급 스펙트럼 분석은 추가 라이브러리 필요 (librosa 등)"
            }
        except:
            return {
                "analysis_available": False,
                "note": "스펙트럼 분석 실패"
            }
    
    def compress_for_stt(self, 
                        input_file: str, 
                        output_file: str, 
                        format: str = "wav") -> Tuple[bool, str]:
        """
        STT(음성인식) 최적화 압축
        
        Args:
            input_file: 입력 파일 경로
            output_file: 출력 파일 경로
            format: 출력 포맷 (wav, mp3, aac)
        
        Returns:
            (성공 여부, 메시지)
        """
        if not os.path.exists(input_file):
            return False, f"❌ 입력 파일을 찾을 수 없습니다: {input_file}"
        
        # 출력 디렉토리 생성
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        
        # STT 최적화 설정
        cmd = [self.ffmpeg_path, "-y", "-i", input_file]
        
        if format.lower() == "wav":
            # WAV: 무손실, 16kHz, 모노
            cmd.extend([
                "-ar", "16000",  # 16kHz 샘플레이트
                "-ac", "1",      # 모노 채널
                "-acodec", "pcm_s16le"  # 16-bit PCM
            ])
        elif format.lower() == "mp3":
            # MP3: 192kbps, 16kHz, 모노
            cmd.extend([
                "-ar", "16000",  # 16kHz 샘플레이트
                "-ac", "1",      # 모노 채널
                "-codec:a", "libmp3lame",
                "-b:a", "192k"   # 192kbps
            ])
        elif format.lower() == "aac":
            # AAC: 128kbps, 16kHz, 모노
            cmd.extend([
                "-ar", "16000",  # 16kHz 샘플레이트
                "-ac", "1",      # 모노 채널
                "-codec:a", "aac",
                "-b:a", "128k"   # 128kbps
            ])
        else:
            return False, f"❌ STT용으로 지원하지 않는 포맷: {format}"
        
        cmd.extend(["-loglevel", "error", output_file])
        
        try:
            print(f"🔄 STT 최적화 압축 중... {os.path.basename(input_file)} → {os.path.basename(output_file)}")
            print(f"   포맷: {format.upper()}, 샘플레이트: 16kHz, 채널: 모노")
            
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            
            if os.path.exists(output_file):
                original_size = os.path.getsize(input_file)
                compressed_size = os.path.getsize(output_file)
                compression_ratio = (1 - compressed_size / original_size) * 100
                
                return True, f"✅ STT 최적화 완료! 용량 {compression_ratio:.1f}% 감소 ({original_size/1024/1024:.1f}MB → {compressed_size/1024/1024:.1f}MB)"
            else:
                return False, "❌ STT 최적화된 파일이 생성되지 않았습니다"
                
        except subprocess.CalledProcessError as e:
            return False, f"❌ STT 최적화 실패: {e.stderr}"
        except Exception as e:
            return False, f"❌ 오류 발생: {e}"

    def compress_audio(self, 
                      input_file: str, 
                      output_file: str, 
                      format: str = "mp3",
                      preset: str = "standard",
                      custom_bitrate: Optional[str] = None) -> Tuple[bool, str]:
        """
        오디오 파일 압축
        
        Args:
            input_file: 입력 파일 경로
            output_file: 출력 파일 경로
            format: 출력 포맷 (mp3, aac, ogg, flac)
            preset: 압축 프리셋 (high_quality, standard, compressed, low_size)
            custom_bitrate: 사용자 정의 비트레이트 (예: "128k")
        
        Returns:
            (성공 여부, 메시지)
        """
        if not os.path.exists(input_file):
            return False, f"❌ 입력 파일을 찾을 수 없습니다: {input_file}"
        
        # 출력 디렉토리 생성
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        
        # 비트레이트 설정
        if custom_bitrate:
            bitrate = custom_bitrate
        elif format in self.compression_presets.get(preset, {}):
            bitrate = self.compression_presets[preset][format]["bitrate"]
        else:
            bitrate = "128k"  # 기본값
        
        # FFmpeg 명령어 구성
        cmd = [self.ffmpeg_path, "-y", "-i", input_file]
        
        if format.lower() == "mp3":
            cmd.extend(["-codec:a", "libmp3lame", "-b:a", bitrate])
        elif format.lower() == "aac":
            cmd.extend(["-codec:a", "aac", "-b:a", bitrate])
        elif format.lower() == "ogg":
            cmd.extend(["-codec:a", "libvorbis", "-b:a", bitrate])
        elif format.lower() == "flac":
            cmd.extend(["-codec:a", "flac"])  # FLAC은 무손실이므로 비트레이트 불필요
        else:
            return False, f"❌ 지원하지 않는 포맷: {format}"
        
        cmd.extend(["-loglevel", "error", output_file])
        
        try:
            print(f"🔄 압축 중... {os.path.basename(input_file)} → {os.path.basename(output_file)}")
            print(f"   포맷: {format.upper()}, 비트레이트: {bitrate}")
            
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            
            # 압축 결과 확인
            if os.path.exists(output_file):
                original_size = os.path.getsize(input_file)
                compressed_size = os.path.getsize(output_file)
                compression_ratio = (1 - compressed_size / original_size) * 100
                
                return True, f"✅ 압축 완료! 용량 {compression_ratio:.1f}% 감소 ({original_size/1024/1024:.1f}MB → {compressed_size/1024/1024:.1f}MB)"
            else:
                return False, "❌ 압축된 파일이 생성되지 않았습니다"
                
        except subprocess.CalledProcessError as e:
            return False, f"❌ 압축 실패: {e.stderr}"
        except Exception as e:
            return False, f"❌ 오류 발생: {e}"
    
    def batch_compress(self, 
                      input_dir: str, 
                      output_dir: str,
                      formats: List[str] = ["mp3"],
                      preset: str = "standard",
                      file_extensions: List[str] = [".mp3", ".m4a", ".wav", ".flac", ".aac"]) -> dict:
        """
        배치 압축 처리
        
        Args:
            input_dir: 입력 디렉토리
            output_dir: 출력 디렉토리
            formats: 압축할 포맷 리스트
            preset: 압축 프리셋
            file_extensions: 처리할 파일 확장자 리스트
        
        Returns:
            처리 결과 통계
        """
        if not os.path.exists(input_dir):
            return {"error": f"입력 디렉토리를 찾을 수 없습니다: {input_dir}"}
        
        results = {
            "total_files": 0,
            "successful": 0,
            "failed": 0,
            "details": []
        }
        
        # 지원하는 파일 찾기
        input_path = Path(input_dir)
        audio_files = []
        
        for ext in file_extensions:
            audio_files.extend(input_path.glob(f"*{ext}"))
            audio_files.extend(input_path.glob(f"*{ext.upper()}"))
        
        results["total_files"] = len(audio_files)
        
        print(f"📁 {len(audio_files)}개의 오디오 파일을 찾았습니다")
        
        for file_path in audio_files:
            print(f"\n📄 처리 중: {file_path.name}")
            
            # 원본 파일 정보
            original_info = self.get_file_info(str(file_path))
            print(f"   원본: {original_info.get('file_size_mb', 0):.1f}MB")
            
            for format in formats:
                # 출력 파일명 생성
                output_filename = file_path.stem + f"_compressed.{format}"
                output_path = Path(output_dir) / output_filename
                
                # 압축 실행
                success, message = self.compress_audio(
                    str(file_path), 
                    str(output_path), 
                    format, 
                    preset
                )
                
                if success:
                    results["successful"] += 1
                    print(f"   ✅ {format.upper()}: {message}")
                    
                    # 압축된 파일 정보
                    compressed_info = self.get_file_info(str(output_path))
                    print(f"   압축: {compressed_info.get('file_size_mb', 0):.1f}MB")
                else:
                    results["failed"] += 1
                    print(f"   ❌ {format.upper()}: {message}")
                
                results["details"].append({
                    "input_file": str(file_path),
                    "output_file": str(output_path),
                    "format": format,
                    "success": success,
                    "message": message
                })
        
        return results
    
    def show_presets(self):
        """사용 가능한 압축 프리셋 표시"""
        print("\n🎵 사용 가능한 압축 프리셋:")
        print("=" * 50)
        
        for preset_name, formats in self.compression_presets.items():
            print(f"\n📌 {preset_name.upper()}:")
            for format_name, settings in formats.items():
                print(f"   {format_name.upper()}: {settings['bitrate']} - {settings['description']}")
    
    def print_quality_analysis(self, analysis_result: dict):
        """음질 분석 결과를 보기 좋게 출력"""
        if "error" in analysis_result:
            print(f"❌ 분석 실패: {analysis_result['error']}")
            return
        
        basic_info = analysis_result["basic_info"]
        quality_grade = analysis_result["quality_grade"]
        recommendations = analysis_result["recommendations"]
        
        print("\n🎵 오디오 파일 음질 분석 결과")
        print("=" * 60)
        
        # 기본 정보
        print(f"\n📄 파일 정보:")
        print(f"   크기: {basic_info.get('file_size_mb', 0):.1f} MB")
        print(f"   길이: {basic_info.get('duration', 0):.1f} 초")
        print(f"   코덱: {basic_info.get('codec', 'unknown').upper()}")
        
        # 음질 등급
        print(f"\n🎚️ 음질 등급: {quality_grade['overall_grade']} (점수: {quality_grade['overall_score']}/10)")
        
        # 상세 평가
        print(f"\n📊 상세 평가:")
        print(f"   비트레이트: {quality_grade['bitrate']['value']//1000}kbps ({quality_grade['bitrate']['grade']})")
        print(f"   샘플레이트: {quality_grade['sample_rate']['value']}Hz ({quality_grade['sample_rate']['grade']})")
        print(f"   채널: {quality_grade['channels']['value']} ({quality_grade['channels']['grade']})")
        print(f"   코덱 품질: {quality_grade['codec']['score']}/10")
        
        # 압축 권장사항
        print(f"\n💡 압축 권장사항:")
        print(f"   압축 권장: {'✅ 예' if recommendations['should_compress'] else '❌ 아니오'}")
        print(f"   이유: {recommendations['reason']}")
        
        if recommendations['should_compress']:
            print(f"\n🎯 권장 설정:")
            print(f"   권장 프리셋: {', '.join(recommendations['recommended_presets'])}")
            print(f"   권장 비트레이트: {recommendations['custom_bitrate']}")
            
            if recommendations['avoid_presets']:
                print(f"   피해야 할 프리셋: {', '.join(recommendations['avoid_presets'])}")
            
            # 예상 절약량
            if recommendations['estimated_savings']:
                print(f"\n💰 예상 용량 절약:")
                for preset, savings in recommendations['estimated_savings'].items():
                    print(f"   {preset}: {savings['target_bitrate']} → {savings['estimated_size_mb']}MB ({savings['savings_percent']}% 절약)")
        
        # STT 최적화 권장사항
        print(f"\n🎤 STT(음성인식) 최적화 권장사항:")
        stt_recommendation = self._get_stt_recommendation(quality_grade, basic_info)
        print(f"   STT 최적화: {'✅ 권장' if stt_recommendation['recommended'] else '❌ 불필요'}")
        print(f"   이유: {stt_recommendation['reason']}")
        if stt_recommendation['recommended']:
            print(f"   권장 포맷: {stt_recommendation['recommended_format']}")
            print(f"   예상 STT 정확도: {stt_recommendation['expected_accuracy']}")
            print(f"   용량 절약: {stt_recommendation['size_savings']}")
        
        print(f"\n⏰ 분석 시간: {analysis_result['analysis_timestamp']}")
    
    def _get_stt_recommendation(self, quality_grade: dict, basic_info: dict) -> dict:
        """STT 최적화 권장사항 생성"""
        bitrate = quality_grade['bitrate']['value']
        sample_rate = quality_grade['sample_rate']['value']
        channels = quality_grade['channels']['value']
        file_size_mb = basic_info.get('file_size_mb', 0)
        
        recommendation = {
            "recommended": False,
            "reason": "",
            "recommended_format": "",
            "expected_accuracy": "",
            "size_savings": ""
        }
        
        # STT 최적화 필요성 판단
        needs_optimization = False
        reason_parts = []
        
        # 샘플레이트 확인 (16kHz가 최적)
        if sample_rate > 16000:
            needs_optimization = True
            reason_parts.append(f"샘플레이트 {sample_rate}Hz → 16kHz 최적화")
        
        # 채널 확인 (모노가 최적)
        if channels > 1:
            needs_optimization = True
            reason_parts.append(f"스테레오 → 모노 변환")
        
        # 비트레이트 확인 (192kbps 이하 권장)
        if bitrate > 192000:
            needs_optimization = True
            reason_parts.append(f"비트레이트 {bitrate//1000}kbps → 192kbps 이하")
        
        if needs_optimization:
            recommendation["recommended"] = True
            recommendation["reason"] = "; ".join(reason_parts)
            
            # 권장 포맷 결정
            if file_size_mb > 50:  # 큰 파일
                recommendation["recommended_format"] = "WAV (무손실, 16kHz 모노)"
                recommendation["expected_accuracy"] = "95-98%"
                recommendation["size_savings"] = "약 30-50% 절약"
            elif file_size_mb > 10:  # 중간 파일
                recommendation["recommended_format"] = "MP3 (192kbps, 16kHz 모노)"
                recommendation["expected_accuracy"] = "90-95%"
                recommendation["size_savings"] = "약 50-70% 절약"
            else:  # 작은 파일
                recommendation["recommended_format"] = "AAC (128kbps, 16kHz 모노)"
                recommendation["expected_accuracy"] = "85-90%"
                recommendation["size_savings"] = "약 60-80% 절약"
        else:
            recommendation["reason"] = "이미 STT에 최적화된 설정"
            recommendation["expected_accuracy"] = "90-95%"
        
        return recommendation

    def analyze_and_recommend(self, file_path: str):
        """파일을 분석하고 압축 권장사항을 출력"""
        print(f"🔍 분석 중: {os.path.basename(file_path)}")
        analysis_result = self.analyze_audio_quality(file_path)
        self.print_quality_analysis(analysis_result)
        return analysis_result

def main():
    parser = argparse.ArgumentParser(description="오디오 파일 자동 압축 도구")
    parser.add_argument("input", help="입력 파일 또는 디렉토리")
    parser.add_argument("-o", "--output", help="출력 파일 또는 디렉토리")
    parser.add_argument("-f", "--format", default="mp3", 
                       choices=["mp3", "aac", "ogg", "flac"],
                       help="출력 포맷 (기본값: mp3)")
    parser.add_argument("-p", "--preset", default="standard",
                       choices=["high_quality", "standard", "compressed", "low_size"],
                       help="압축 프리셋 (기본값: standard)")
    parser.add_argument("-b", "--bitrate", help="사용자 정의 비트레이트 (예: 128k)")
    parser.add_argument("--batch", action="store_true", help="배치 처리 모드")
    parser.add_argument("--presets", action="store_true", help="사용 가능한 프리셋 표시")
    parser.add_argument("--analyze", action="store_true", help="음질 분석만 수행 (압축하지 않음)")
    parser.add_argument("--stt", action="store_true", help="STT(음성인식) 최적화 압축")
    parser.add_argument("--ffmpeg-path", default="/opt/homebrew/bin/ffmpeg",
                       help="FFmpeg 실행 파일 경로")
    
    args = parser.parse_args()
    
    # 압축기 초기화
    compressor = AudioCompressor(args.ffmpeg_path)
    
    # FFmpeg 확인
    if not compressor.check_ffmpeg():
        return
    
    # 프리셋 표시
    if args.presets:
        compressor.show_presets()
        return
    
    # 입력 경로 확인
    if not os.path.exists(args.input):
        print(f"❌ 입력 경로를 찾을 수 없습니다: {args.input}")
        return
    
    # 음질 분석만 수행
    if args.analyze:
        if os.path.isfile(args.input):
            compressor.analyze_and_recommend(args.input)
        else:
            print("❌ 분석 모드는 단일 파일에만 사용 가능합니다")
        return
    
    # STT 최적화 압축
    if args.stt:
        if os.path.isfile(args.input):
            if not args.output:
                input_path = Path(args.input)
                args.output = str(input_path.parent / f"{input_path.stem}_stt_optimized.{args.format}")
            
            success, message = compressor.compress_for_stt(args.input, args.output, args.format)
            print(f"\n{message}")
        else:
            print("❌ STT 최적화 모드는 단일 파일에만 사용 가능합니다")
        return
    
    # 출력 경로 설정
    if not args.output:
        if os.path.isfile(args.input):
            input_path = Path(args.input)
            args.output = str(input_path.parent / f"{input_path.stem}_compressed.{args.format}")
        else:
            args.output = f"{args.input}_compressed"
    
    print(f"🎵 오디오 압축기 시작")
    print(f"📁 입력: {args.input}")
    print(f"📁 출력: {args.output}")
    print(f"🎚️ 포맷: {args.format.upper()}")
    print(f"⚙️ 프리셋: {args.preset}")
    if args.bitrate:
        print(f"🎛️ 비트레이트: {args.bitrate}")
    
    # 배치 처리
    if args.batch or os.path.isdir(args.input):
        results = compressor.batch_compress(
            args.input, 
            args.output, 
            [args.format], 
            args.preset
        )
        
        print(f"\n📊 배치 처리 완료:")
        print(f"   총 파일: {results['total_files']}")
        print(f"   성공: {results['successful']}")
        print(f"   실패: {results['failed']}")
    
    # 단일 파일 처리
    else:
        success, message = compressor.compress_audio(
            args.input, 
            args.output, 
            args.format, 
            args.preset, 
            args.bitrate
        )
        print(f"\n{message}")

if __name__ == "__main__":
    main()
