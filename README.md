# 주식 유튜브 자동요약 대시보드 (1단계 + 2단계 MVP)

내 컴퓨터를 켜두지 않아도, GitHub의 서버가 1시간마다 알아서
1) 지정한 유튜브 채널에 새 영상이 올라왔는지 확인하고
2) 자막을 가져와서 Claude(AI)로 요약하고
3) 결과를 웹페이지(대시보드)에 저장

해주는 시스템입니다. 아래 순서대로 딱 한 번만 설정하면, 그 뒤로는 폰이나 회사 컴퓨터 브라우저로 주소만 열어보면 됩니다.

지금 버전은 **1단계(유튜브 자동 요약) + 2단계(대시보드)** 까지입니다.
3단계(차트 패턴 스크리닝)와 4단계(완전 자동화)는 대시보드에 "준비 중"으로 표시되어 있고, 다음 작업에서 이어서 만들면 됩니다.

---

## 준비물 (모두 무료로 시작 가능)

| 항목 | 용도 | 발급처 |
|---|---|---|
| Anthropic(Claude) API 키 | 자막을 읽고 요약하는 AI | https://console.anthropic.com |
| Supadata API 키 | 유튜브 자막 추출 | https://supadata.ai |
| GitHub 계정 | 이미 만드셨음 ✅ | - |

> **API 키란?** "이 프로그램이 나 대신 이 서비스를 써도 좋다"는 개인 비밀번호 같은 것입니다. 본인 명의로만 발급되기 때문에 직접 가입/발급해야 합니다. 아래에 화면 순서대로 적어뒀습니다.

---

## 0단계. 이 폴더 확인

이 프로젝트는 아래 폴더에 만들어져 있습니다.

```
C:\Users\phy11\Documents\stock-dashboard
```

폴더 구조:
- `scripts/` : 파이썬 자동화 코드 (RSS 체크 → 자막 추출 → AI 요약)
- `docs/` : 웹 대시보드 (GitHub Pages로 공개됨)
- `.github/workflows/pipeline.yml` : 1시간마다 자동 실행되는 스케줄러 설정

---

## 1단계. Anthropic(Claude) API 키 발급

1. https://console.anthropic.com 접속 후 가입/로그인
2. 왼쪽 메뉴에서 **API Keys** 클릭 → **Create Key** 클릭
3. 만들어진 키 (`sk-ant-...`로 시작) 복사해서 메모장에 잠시 저장
4. **Billing** 메뉴에서 최소 금액(예: $5) 결제 필요 — 요약 1건당 비용은 매우 저렴합니다(보통 몇 원~수십 원 수준)

## 2단계. Supadata API 키 발급

1. https://supadata.ai 접속 후 가입/로그인
2. 대시보드에서 **API Key** 발급/복사 (무료 크레딧 제공)
3. 메모장에 잠시 저장

---

## 3단계. GitHub에 저장소(repository) 만들기

1. https://github.com/new 접속
2. Repository name에 `stock-dashboard` 입력 (다른 이름도 가능)
3. **Public** 또는 **Private** 선택 (Private이어도 GitHub Pages 무료로 됩니다)
4. 나머지는 그대로 두고 **Create repository** 클릭
5. 만들어진 저장소 페이지의 초록색 **Code** 버튼 옆 주소를 기억해두세요 (예: `https://github.com/내아이디/stock-dashboard`)

---

## 4단계. 이 폴더를 GitHub로 올리기 (GitHub Desktop 사용 — 가장 쉬움)

지금 컴퓨터에는 git 프로그램이 설치되어 있지 않아서, 명령어 대신 **GitHub Desktop**이라는 클릭 몇 번으로 되는 프로그램을 추천합니다.

1. https://desktop.github.com 에서 다운로드 후 설치
2. 실행 후 방금 만든 Google 계정 연동 GitHub 계정으로 로그인
3. 상단 메뉴 **File → Add local repository** 클릭
4. 아래 경로를 입력/선택:
   ```
   C:\Users\phy11\Documents\stock-dashboard
   ```
5. "This directory does not appear to be a Git repository. Would you like to create one?" 라는 메시지가 뜨면 **create a repository** 클릭
6. 왼쪽 하단에 커밋 메시지(예: "첫 커밋")를 적고 **Commit to main** 클릭
7. 상단의 **Publish repository** 클릭
   - Name을 3단계에서 만든 저장소 이름과 동일하게 맞추거나, 여기서 새로 만들어도 됩니다
   - "Keep this code private" 체크 여부는 원하는 대로 선택
8. Publish 완료되면 GitHub 웹사이트에 코드가 올라간 것을 확인할 수 있습니다

---

## 5단계. GitHub 저장소에 API 키 등록 (Secrets)

1. GitHub에서 방금 만든 저장소로 이동
2. **Settings** 탭 클릭
3. 왼쪽 메뉴에서 **Secrets and variables → Actions** 클릭
4. **New repository secret** 클릭해서 아래 2개를 각각 등록:
   - Name: `ANTHROPIC_API_KEY` / Secret: 1단계에서 받은 키
   - Name: `SUPADATA_API_KEY` / Secret: 2단계에서 받은 키

---

## 6단계. 자동 커밋을 위한 권한 설정

1. 저장소 **Settings → Actions → General** 이동
2. 아래쪽 **Workflow permissions** 항목에서
   **"Read and write permissions"** 로 변경 → **Save**
   (이 설정이 없으면 자동화가 결과를 저장하지 못합니다)

---

## 7단계. GitHub Pages 켜기 (대시보드 공개 주소 만들기)

1. 저장소 **Settings → Pages** 이동
2. **Source**: `Deploy from a branch` 선택
3. **Branch**: `main` / 폴더: `/docs` 선택 → **Save**
4. 1~2분 후 페이지 상단에 대시보드 주소가 표시됩니다:
   ```
   https://내아이디.github.io/stock-dashboard/
   ```
   이 주소를 폰/회사 컴퓨터 브라우저 즐겨찾기에 저장해두면 됩니다.

---

## 8단계. 첫 실행 테스트

1. 저장소 **Actions** 탭 이동
2. 왼쪽에서 **stock-youtube-pipeline** 클릭
3. 오른쪽 **Run workflow** 버튼 클릭 → 다시 **Run workflow** 클릭
4. 1~2분 기다린 후 초록색 체크가 뜨면 성공

> ⚠️ **첫 실행에서는 요약이 생성되지 않습니다.** 처음에는 각 채널의 최근 영상들을 "이미 확인함" 상태로만 기록해서, 과거 영상들을 한꺼번에 요약하며 API 비용이 많이 나가는 것을 막습니다. **그 다음부터 새로 올라오는 영상만 자동으로 요약**됩니다. 바로 확인해보고 싶다면 `docs/data/state.json` 파일에서 특정 채널의 channel_id 항목을 지우고 다시 실행하면 그 채널만 새로 감지됩니다.

이후로는 매시 정각(UTC 기준, 한국시간 기준 정시)에 자동으로 실행됩니다. 컴퓨터를 꺼둬도 GitHub 서버에서 돌아갑니다.

---

## 대시보드 사용법

- **유튜브 요약** 탭: 채널별 새 영상 요약, 하루 지난 영상은 자동으로 접힘(제목 클릭하면 펼쳐짐)
- 여러 채널이 같은 종목을 언급하면 상단에 노란색 박스로 하이라이트
- **차트 스크리닝 결과** 탭: 3단계 기능 추가 전까지는 "준비 중" 표시

---

## 다음 단계 (추가 예정)

- 3단계: 차트 패턴 스크리닝 (한국주식 pykrx / 미국주식 yfinance 연동)
- 4단계: 스크리닝 결과와 유튜브 언급 종목 교차 하이라이트 확장

준비되면 이어서 진행하면 됩니다.
