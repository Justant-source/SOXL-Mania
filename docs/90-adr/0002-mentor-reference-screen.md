# ADR 0002: 멘토 레퍼런스 화면

## 상태
승인됨

## 배경
멘토 백테스트 이미지는 `/backtests` 멘토 매트릭스의 권위 있는 화면 레퍼런스다. 저장소에는 이미 부분 전사 JSON이 있었지만, 현재 런타임 엔진과 로컬 SOXL 스냅샷은 이 이미지에 대한 parity를 만족하지 않는다.

Phase A에서는 런타임 코드에서 현재 엔진 동작을 직접 검증했다.

- `year_boundary`는 `StrategyConfig`에 존재하지만 `run_strategy()`는 이를 소비하지 않는다. 현재 동작은 사실상 모든 실행에서 `carry`다.
- `end_of_test=force_close`는 전체 백테스트 종료 시점에만 구현되어 있으며, 연도 경계에는 적용되지 않는다.
- `sizing_mode=fixed_principal`는 신규 진입마다 초기 스레드 원금을 사용한다.
- `sizing_mode=thread_compound`는 신규 진입마다 각 스레드의 현재 free equity를 사용한다.
- `sizing_mode=portfolio_rebalance_compound`는 신규 진입마다 `total_equity / thread_count`를 사용한다.
- 현재 변동성 helper는 일별 equity 수익률의 모집단 표준편차를 계산하는데, 이것은 멘토 매트릭스의 연간 수익률 표준편차 행과 같은 지표가 아니다.
- `price_basis=adjusted_close`는 `MarketBar.adj_close`를 읽지만, 현재 표준 로컬 스냅샷은 전체 구간에서 `adj_close`가 `close`를 그대로 복제하기 때문에 adjusted-close parity를 지원하지 못한다는 경고를 낸다.

2026-06-19 기준 로컬 스냅샷 관측 상태:

- 전체 스냅샷 `data_hash`: `87c5a8bd35006c7a2624d99d0609ba5302e97807d332a567db121be1ff668aca`
- 2011-2024 구간 `data_hash`: `ce269809b8ce2eb0140935980842607bb21c9e994d43983b38f3e3109245633f`
- `mentor_default_5x30`에 대한 `profile show` 결과는 `price_basis=adjusted_close`, `execution_model=ideal_same_close`, `sizing_mode=fixed_principal`, `year_boundary=carry`
- 레거시 멘토 fixture에 대한 `parity report`는 연간 경계 가격에서 `DATA_MISMATCH`, 연간 수익률과 카운트에서 `FAIL`을 반환한다.
- 연간 경계 가격 비교는 표시 반올림 노이즈를 `±0.01`로 허용해도 깨진다.
  - 첫 의미 있는 경계 불일치: `2022` 연말 `2022-12-30`, expected `9.36`, actual `9.67`
  - 다음 경계 불일치: `2023` 연말 `2023-12-29`, expected `28.04`, actual `31.40`

## 결정
멘토 레퍼런스 화면은 두 개의 실행 계열을 사용한다.

1. Block B 연간 수익률, Block B 표준편차/평균, Block D 연도별 카운트 행에 사용하는 연도 독립 계열
2. Block C 단리/복리 집계 행과 Block D 집계 카운트 행에 사용하는 연속 carry 계열

목표 의미론 매핑:

- Block B와 Block D의 연도별 행:
  - 연도별 독립 실행
  - 기준 자본: `$10,000`
  - 목표 의미론 라벨: `year_boundary=reset`
- Block C 단리 행:
  - 선택한 윈도에 대한 연속 실행
  - `sizing_mode=fixed_principal`
- Block C 복리 행:
  - 선택한 윈도에 대한 연속 실행
  - `sizing_mode=thread_compound`
- 연속 윈도:
  - `total`: `2011-01-01 .. 2024-12-31`
  - `y5`: `2020-01-01 .. 2024-12-31`
  - `y3`: `2022-01-01 .. 2024-12-31`
  - `y1`: `2024-01-01 .. 2024-12-31`
- 멘토 매트릭스의 연간 표준편차:
  - 연간 수익률 퍼센트에서 계산
  - 이후 parity 증거가 다른 공식을 지지하지 않는 한 모집단 표준편차 사용

이 화면을 위한 레퍼런스 무결성 규칙:

- 권위 있는 표시 fixture는 `engine/tests/fixtures/mentor_reference_matrix.yaml`에 고정한다.
- 런타임 payload는 실제 계산값과 parity 상태를 포함할 수 있지만, `data_hash`가 권위 있는 원본 스냅샷과 다르면 절대 parity를 `PASS`로 표시하지 않는다.
- 권위 있는 adjusted-close 데이터셋이 복구되기 전까지, 대시보드는 런타임 실제값을 기본으로 표시하고 고정된 멘토 전사값은 비교용 메타데이터로만 노출한다.

허용 오차 정책:

- 연간 수익률 셀: `±0.1` 퍼센트포인트
- 집계 수익률 셀: 동일 `data_hash`를 가진 parity fixture가 더 좁은 허용 오차를 정당화하지 않는 한 `±0.5` 퍼센트포인트
- 카운트 행: 합계 행은 정수 일치, 평균 행은 `±0.5`

## 결과
- 기존 연도 요약과 grid 출력만으로는 충분하지 않으므로, 엔진에는 별도 mentor matrix 리포트 경로가 필요하다.
- 현재 엔진은 `year_boundary`를 무시하므로, mentor matrix 계산을 위해서는 `year_boundary`를 실제 의미론으로 구현해야 한다.
- 현재 데이터에서 연도별 독립 실행을 별도로 계산해도 멘토 값과 완전히 수렴하지 않는다.
  - `5x30`, `ideal_same_close`, 연도별 독립 실행 기준 `2023=71.43`, `2024=81.81`로 멘토 값에 가까워지지만
  - 같은 조건의 `2020=321.49`, 카운트 `95/5`는 멘토 `66.8`, `112/6`와 크게 다르다.
  - 따라서 현재 갭은 연도 경계 처리만의 문제가 아니라 데이터셋 또는 추가 의미론 차이를 포함한다.
- 대시보드 렌더링과 parity 테스트는 세 가지 상태를 구분해야 한다.
  - `PASS`: 값이 맞고 `data_hash`도 일치
  - `FAIL`: 권위 있는 데이터셋을 사용했지만 값이 다름
  - `DATA_MISMATCH`: 현재 데이터셋으로는 parity 자체를 주장할 수 없음
