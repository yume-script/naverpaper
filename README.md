# 네이버 폐지 줍기 (독립 실행 / cron용)

flaskfarm 플러그인 `ff_naverpaper`를 플러그인 없이 단독으로 실행하도록 바꾼 버전입니다.
수집 엔진(`source_naverpaper.py`)은 원본 그대로이고, 실행부만 `naverpaper.py`(CLI)로 대체했습니다.

## 설치

```bash
cd naverpaper
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
# 리눅스에서 브라우저 의존 라이브러리가 없다면
playwright install-deps chromium

cp config.example.json config.json
chmod 600 config.json     # 비밀번호가 들어 있으니 권한 제한 권장
nano config.json
```

## 설정 (config.json)

| 키 | 설명 |
| --- | --- |
| accounts[].user_id / password | 네이버 계정 (여러 개 가능) |
| accounts[].nid_aut / nid_ses / nid_jst | 선택. 브라우저에서 복사한 쿠키를 직접 넣을 때 (저장된 쿠키가 없거나 만료됐을 때만 사용) |
| no_paper_record | 방문할 URL이 없을 때도 결과 DB에 기록 |
| keep_campaign_days / keep_user_days | 오래된 기록 보관 일수 (기본 60 / 7) |
| proxy_url, proxy_reward_enabled, proxy_login_enabled | 프록시 사용 여부 |
| data_dir | 쿠키·DB·로그 저장 위치 (비우면 `./data`) |

## 사용

```bash
python3 naverpaper.py              # 1회 수집 후 종료 (= run)
python3 naverpaper.py notify       # 적립 합계를 디스코드로 전송 (--test 는 테스트 메시지)
python3 naverpaper.py total        # 적립 합계(오늘/7일/30일/전체) + 최근 실행 내역 (-n 20 으로 개수 지정)
python3 naverpaper.py status       # 계정별 쿠키 상태
python3 naverpaper.py login 아이디  # 캡차로 자동 로그인이 막힐 때 (터미널에서 직접)
python3 naverpaper.py link URL     # 링크 하나만 수동 적립
```

`login` 은 로그인 화면을 `data/login_<아이디>.png` 로 저장합니다. 이미지를 보고 캡차 문자를 입력하면 쿠키가 저장되고, 이후 cron 실행에서는 저장된 쿠키를 씁니다.

## 적립 금액 확인

- 수집이 끝날 때마다 로그와 화면에 **이번 실행 적립**(계정별 `+N`)과 **계정별 오늘 / 7일 / 30일 / 전체 합계**가 출력됩니다.
- 언제든 `python3 naverpaper.py total` 로 다시 볼 수 있습니다.
- 적립액은 수집 전후 네이버페이 포인트 **잔액 차이**로 계산한 값입니다. 수집 중 다른 곳에서 포인트를 쓰거나 받으면 오차가 생길 수 있고, 잔액을 읽지 못하면 0으로 기록됩니다.
- 합계는 `data/naverpaper.sqlite`에 쌓인 기록 기준이라, 기존 플러그인 DB를 복사해 오면 이전 기록까지 합산됩니다.

## 디스코드 알림

`config.json`에 웹훅 주소를 넣으면 수집이 끝날 때마다 이번 적립과 누적 합계가 전송됩니다.

```json
"discord_webhook_url": "https://discord.com/api/webhooks/웹훅ID/토큰?thread_id=스레드ID",
"discord_notify_mode": "always",
"discord_mask_ids": true
```

| 키 | 설명 |
| --- | --- |
| discord_webhook_url | 웹훅 주소. 스레드로 보내려면 끝에 `?thread_id=스레드ID`를 그대로 붙입니다. 환경변수 `NAVERPAPER_DISCORD_WEBHOOK`로도 지정할 수 있습니다. |
| discord_notify_mode | `always` 매번 전송 / `earned` 적립이 있거나 오류일 때만 전송 (오류는 항상 전송) |
| discord_mask_ids | true 면 아이디를 앞 3글자만 표시 (`abc•••`) |

- 먼저 `python3 naverpaper.py notify --test` 로 연결을 확인하세요.
- `python3 naverpaper.py notify` 는 수집 없이 현재 합계만 보냅니다. 하루 한 번 요약을 받고 싶다면 cron에 따로 등록하면 됩니다.
- 웹훅 주소에는 토큰이 들어 있어 주소를 아는 사람은 누구나 글을 올릴 수 있습니다. `config.json` 권한을 600으로 두고 외부에 공유하지 마세요. 로그에는 주소를 남기지 않습니다.
- 전송이 실패해도 수집 결과와 종료 코드에는 영향이 없습니다(로그에 오류만 기록).

## cron 등록

원래 플러그인 기본 스케줄(`0 3,9,15,21 * * *`)과 같은 예시입니다.

```cron
0 3,9,15,21 * * * cd /경로/naverpaper && /경로/naverpaper/venv/bin/python naverpaper.py >> /dev/null 2>&1
```

- 로그는 `data/naverpaper.log` 에 자동 저장됩니다(2MB x 4개 순환).
- 이전 실행이 끝나기 전에 다시 cron이 돌면 파일 락으로 건너뜁니다(종료 코드 2).
- 종료 코드: 0 정상 / 1 수집 오류 / 2 중복 실행으로 건너뜀 / 3 설정·패키지 오류

## 데이터 위치

```
data/
  naverpaper.sqlite   수집·방문 기록 DB
  cookies/<아이디>.json
  browser_profiles/   로그인용 브라우저 프로필
  naverpaper.log
```

기존 플러그인 데이터를 이어 쓰려면 플러그인의 `data/` 폴더 내용(`naverpaper.sqlite`, `cookies/`, `browser_profiles/`)을 이 `data/`로 복사하세요.
