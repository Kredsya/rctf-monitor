# rCTF Monitor

rCTF 대회의 문제·첨부·풀이 현황·순위를 한 화면에서 확인하는 Linux 터미널 모니터입니다.
팀 토큰으로 로그인하며, CPU/RAM 그래프와 Codex 사용량도 표시합니다.

```bash
git clone https://github.com/Kredsya/rctf-monitor.git
cd rctf-monitor
python3 install.py
python3 rctf-monitor.py
```

Python 3.12 이상. 모니터는 외부 Python 패키지 없이 실행됩니다.
설치 중 대회 주소와 팀 토큰을 입력하세요. 토큰 로그인 URL도 입력할 수 있습니다.
`22.sh`·`25.sh` 작업창을 사용하려면 `tmux`가 필요합니다.

- 문제와 첨부는 `workspace/<대회명>/<문제명>/`에 저장합니다. 원본 ZIP/TAR도 보관합니다.
- 설정과 토큰은 `.rctf-monitor-local/`에 저장하며 Git에서 제외합니다.
- `Ctrl+C`로 화면을 지우고 종료합니다.
- `python3 rctf-monitor.py --check`: 인증·API 확인만 수행합니다.
- `python3 rctf-monitor.py --once`: 한 번 동기화하고 종료합니다.

[동작·제약·개발 안내](.github/USAGE.md)
