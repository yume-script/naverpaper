# 네이버 폐지 줍기 (독립 실행 / cron용)

flaskfarm 플러그인 [`ff_naverpaper`](https://gitlab.com/sukajds/ff_naverpaper)를 플러그인 없이 단독으로 실행하도록 바꾼 버전입니다.
수집 엔진(`source_naverpaper.py`)은 원본 그대로이고, 실행부만 `naverpaper.py`(CLI)로 대체했습니다.

- cron으로 1회 실행하고 종료
- 실행마다 이번 적립 금액과 계정별 누적 합계 출력
- 디스코드 웹훅(스레드 지정 가능)으로 결과 전송
- 캡차로 자동 로그인이 막힐 때 터미널에서 수동 로그인

---

## 1. 설치 (예: `/mnt/naverpaper`)

### 1-1. 사전 준비

```bash
sudo apt install -y git python3 python3-venv python3-pip
```

### 1-2. 내려받기

```bash
sudo mkdir -p /mnt/naverpaper
sudo chown $USER:$USER /mnt/naverpaper      # root 로 작업 중이면 생략
git clone https://github.com/yume-script/naverpaper.git /mnt/naverpaper
cd /mnt/naverpaper
ls      # naverpaper.py, source_naverpaper.py, requirements.txt 가 보여야 함
```

git 없이 zip 으로 받을 때(`unzip` 필요):

```bash
cd /mnt/naverpaper
wget https://github.com/yume-script/naverpaper/archive/refs/heads/main.zip
unzip main.zip && mv naverpaper-main/* . && rm -r naverpaper-main main.zip
```

기본 브랜치 이름이 `main`이 아니면 주소의 `main` 부분을 바꾸세요.

### 1-3. 폴더가 이미 있을 때

`fatal: destination path '/mnt/naverpaper' already exists and is not an empty directory` 오류가 나면 먼저 안을 확인하세요. 이미 설정한 `config.json`이나 `data/`(쿠키, DB)가 있을 수 있습니다.

```bash
ls -al /mnt/naverpaper
```

| 상황 | 방법 |
| --- | --- |
| 안에 `.git`이 있음 (이미 클론됨) | `cd /mnt/naverpaper && git pull` |
| `.git`이 없고 파일만 있음 | 아래 "코드만 덮어쓰기" |
| 비어 있거나 필요 없는 폴더 | 이름을 바꿔 두고 새로 클론 |

코드만 덮어쓰기 (`config.json`, `data/`는 저장소에 없으므로 그대로 유지됨):

```bash
cp -a /mnt/naverpaper /mnt/naverpaper.bak                      # 불안하면 먼저 백업
git clone https://github.com/yume-script/naverpaper.git /tmp/naverpaper-src
cp -r /tmp/naverpaper-src/. /mnt/naverpaper/
rm -rf /tmp/naverpaper-src
```

필요 없는 폴더를 치우고 새로 받기:

```bash
mv /mnt/naverpaper /mnt/naverpaper.old
git clone https://github.com/yume-script/naverpaper.git /mnt/naverpaper
```

저장소 안에 `naverpaper/` 폴더가 한 겹 더 있다면, 그 폴더의 내용을 `/mnt/naverpaper/`로 복사해서 쓰세요.

### 1-4. 파이썬 환경과 브라우저

```bash
cd /mnt/naverpaper
python3 -m venv venv              # 이미 venv/ 가 있으면 건너뜀
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium  # 브라우저 구동용 시스템 라이브러리 (root 또는 sudo)
```

`install-deps`는 서버에 GUI 라이브러리가 없으면 chromium이 실행되지 않기 때문에 필요합니다.

### 1-5. 설정 파일

```bash
cp -n config.example.json config.json    # config.json 이 이미 있으면 유지
chmod 600 config.json                    # 비밀번호가 들어 있으니 권한 제한
nano config.json
```

### 1-6. 첫 실행

```bash
python3 naverpaper.py status             # 계정별 쿠키 상태 (처음엔 NG 가 정상)
python3 naverpaper.py                    # 1회 수집
```

처음 실행하면 비밀번호로 자동 로그인을 시도합니다. 캡차로 막히면 `python3 naverpaper.py login 아이디` 를 사용하세요(아래 "수동 로그인").

### 1-7. cron 등록

```bash
crontab -e
```

```cron
0 3,9,15,21 * * * cd /mnt/naverpaper && ./venv/bin/python naverpaper.py
```

- 로그는 `data/naverpaper.log` 에 자동 저장됩니다 (2MB x 4개 순환).
- 이전 실행이 끝나기 전에 다시 cron이 돌면 파일 락으로 건너뜁니다 (종료 코드 2).
- `/mnt`가 외장 디스크나 네트워크 마운트라면 부팅 직후 마운트 전에 cron이 돌 수 있습니다. 실행이 안 될 때는 `ls /mnt/naverpaper` 로 마운트 상태를 확인하세요.

### 1-8. 업데이트

```bash
cd /mnt/naverpaper
git pull
source venv/bin/activate
pip install -r requirements.txt     # 의존성이 바뀌었을 때만
```

`config.json`과 `data/`는 저장소에 올라가지 않으므로 그대로 유지됩니다.

---

## 2. 설정 (config.json)

| 키 | 설명 |
| --- | --- |
| accounts[].user_id / password | 네이버 계정 (여러 개 가능) |
| accounts[].keep_login | 로그인 상태 유지 체크 (기본 true) |
| accounts[].nid_aut / nid_ses / nid_jst | 선택. 브라우저에서 복사한 쿠키를 직접 넣을 때 (저장된 쿠키가 없거나 만료됐을 때만 사용) |
| no_paper_record | 방문할 URL이 없을 때도 결과 DB에 기록 (기본 true) |
| keep_campaign_days / keep_user_days | 오래된 캠페인 URL / 사용자 세션 기록 보관 일수 (기본 60 / 7) |
| proxy_url | 프록시 주소 |
| proxy_enabled / proxy_reward_enabled / proxy_login_enabled | 프록시 사용 범위 (전체 / 적립 / 로그인) |
| discord_webhook_url | 디스코드 웹훅 주소 (아래 참고) |
| discord_notify_mode | `always` 매번 전송 / `earned` 적립이 있거나 오류일 때만 전송 |
| discord_mask_ids | true 면 아이디를 앞 3글자만 표시 (`abc•••`) |
| data_dir | 쿠키·DB·로그 저장 위치 (비우면 `./data`) |

---

## 3. 사용법

```bash
python3 naverpaper.py                # 1회 수집 후 종료 (= run)
python3 naverpaper.py total          # 적립 합계 + 최근 실행 내역 (-n 30 으로 개수 지정)
python3 naverpaper.py notify         # 적립 합계를 디스코드로 전송 (수집 없음)
python3 naverpaper.py notify --test  # 디스코드 테스트 메시지
python3 naverpaper.py status         # 계정별 쿠키 상태
python3 naverpaper.py login 아이디    # 수동 로그인 (캡차 대응)
python3 naverpaper.py link URL       # 링크 하나만 수동 적립
python3 naverpaper.py -c 다른설정.json run   # 설정 파일 경로 지정
python3 naverpaper.py -v             # 자세한 로그
```

종료 코드: `0` 정상 / `1` 수집 오류 / `2` 이미 실행 중이라 건너뜀 / `3` 설정·패키지 오류

### 수동 로그인

비밀번호 자동 로그인이 캡차 등으로 막히면 터미널에서 직접 실행하세요(cron 불가).

```bash
python3 naverpaper.py login 아이디
```

로그인 화면이 `data/login_아이디.png` 로 저장됩니다. 이미지를 보고 캡차 문자를 입력하면 쿠키가 저장되고, 이후 cron 실행에서는 저장된 쿠키를 씁니다.

---

## 4. 적립 금액 확인

- 수집이 끝날 때마다 로그와 화면에 **이번 실행 적립**(계정별 `+N`)과 **계정별 오늘 / 7일 / 30일 / 전체 합계**가 출력됩니다.
- 언제든 `python3 naverpaper.py total` 로 다시 볼 수 있습니다.

```
== 적립 합계 (기록 시작: 2026-10-10, 총 3회 실행) ==
계정                          오늘        7일       30일          전체
idA                          7        27        27          37
idB                          3         3         3           8
합계                         10        30        30          45
```

- 적립액은 수집 전후 네이버페이 포인트 **잔액 차이**로 계산한 값입니다. 수집 중 다른 곳에서 포인트를 쓰거나 받으면 오차가 생길 수 있고, 잔액을 읽지 못하면 0으로 기록됩니다.
- 합계는 `data/naverpaper.sqlite`에 쌓인 기록 기준이라, 기존 플러그인 DB를 복사해 오면 이전 기록까지 합산됩니다.

---

## 5. 디스코드 알림

`config.json`에 웹훅 주소를 넣으면 수집이 끝날 때마다 이번 적립과 누적 합계가 임베드 메시지로 전송됩니다. 수집 중 오류가 나면 빨간색으로 오류 내용이 전송됩니다.

```json
"discord_webhook_url": "https://discord.com/api/webhooks/웹훅ID/토큰?thread_id=스레드ID",
"discord_notify_mode": "always",
"discord_mask_ids": true
```

- 스레드로 보내려면 주소 끝에 `?thread_id=스레드ID`(숫자)를 그대로 붙입니다.
- 주소를 파일에 두기 싫으면 환경변수 `NAVERPAPER_DISCORD_WEBHOOK` 로도 지정할 수 있습니다.
- 먼저 `python3 naverpaper.py notify --test` 로 연결을 확인하세요.
- `python3 naverpaper.py notify` 는 수집 없이 현재 합계만 보냅니다. 하루 한 번 요약을 받고 싶다면 cron에 따로 등록하세요.
- 전송이 실패해도 수집 결과와 종료 코드에는 영향이 없습니다 (로그에 오류만 기록, 주소는 로그에 남기지 않음).

**보안 주의**: 웹훅 주소에는 토큰이 들어 있어 주소를 아는 사람은 누구나 해당 스레드에 글을 올릴 수 있습니다. `config.json` 권한을 600으로 두고 외부에 공유하지 마세요. 노출됐다면 디스코드에서 웹훅을 재생성하세요.

---

## 6. 데이터 위치

```
/mnt/naverpaper/
  naverpaper.py            실행 파일
  source_naverpaper.py     수집 엔진 (원본 그대로)
  config.json              설정 (git 에 올리지 않음)
  data/
    naverpaper.sqlite      수집·방문·적립 기록 DB
    cookies/<아이디>.json   로그인 쿠키
    browser_profiles/      로그인용 브라우저 프로필
    naverpaper.log         로그
```

기존 플러그인 데이터를 이어 쓰려면 플러그인의 `data/` 폴더 내용(`naverpaper.sqlite`, `cookies/`, `browser_profiles/`)을 이 `data/`로 복사하세요.

`.gitignore` 에 `config.json`, `data/`, `venv/` 가 들어 있으니 비밀번호·쿠키·웹훅 토큰이 저장소에 올라가지 않습니다. 저장소가 공개라면 이 항목을 지우지 마세요.

---

## 7. 문제 해결

| 증상 | 확인 |
| --- | --- |
| `필수 패키지가 없습니다` | `source venv/bin/activate` 후 `pip install -r requirements.txt`, `playwright install chromium` |
| chromium 실행 오류, 공유 라이브러리 누락 | `playwright install-deps chromium` |
| 계정이 NG 로 나오고 로그인이 안 됨 | `python3 naverpaper.py login 아이디` 로 수동 로그인 |
| 종료 코드 2 | 이전 수집이 아직 실행 중이라 건너뜀. 정상 동작 |
| 디스코드로 안 옴 | `notify --test` 실행 후 로그의 HTTP 상태 확인 (404: 주소 또는 thread_id 오류, 401: 토큰 오류) |
| cron 에서만 실패 | cron 줄에서 `cd` 와 `./venv/bin/python` 경로 확인, `/mnt` 마운트 상태 확인 |
