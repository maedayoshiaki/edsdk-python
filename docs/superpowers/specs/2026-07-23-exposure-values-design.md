# 露出値(Av/Tv/ISO)の柔軟な設定 — 設計ドキュメント

日付: 2026-07-23
対象: `edsdk-python`(youme-town fork)
ステータス: 設計承認済み・実装前

## 背景と目的

EDSDK は絞り(Av)・シャッタースピード(Tv)・ISO をそれぞれ独自コード(1EV = 8 の線形構造)で扱う。
静的テーブル(`edsdk/constants/properties.py` の `Av`/`Tv`/`ISOSpeedCamera`)は全カメラの和集合であり、
実際に設定可能な値はカメラ・レンズごとのサブセット(`GetPropertyDesc` で取得)に限られる。

現状の課題:

1. 非対応値を指定すると `ValueError: Value 45 not supported for PropID.Av` のような生コード表示の分かりにくいエラー
2. 「近い値に丸める」機能がない(レンズが f/4 始まりのとき f/3.5 指定は即エラー)
3. サポート値・現在値を数値(F値・秒・ISO値)として取得する手段がない(表示文字列のみ)
4. 同値異コード問題: 同じ F3.5 でも `0x24 "3.5"` と `0x25 "3.5 (1/3)"` の2コードが存在し、
   カメラは片方しか受け付けない(実機 EOS M6 Mark II は `0x25` のみ対応と確認済み)。
   現行の `_reverse_lookup` は辞書衝突で片方が上書きされ消える

## 実機確認結果(Canon EOS M6 Mark II)

- Av: 17コード(f/3.5〜f/22、レンズ依存)。開放は `0x25 "3.5 (1/3)"` であり `0x24 "3.5"` は非対応
- Tv: 53コード(Bulb〜1/4000)
- コード体系は Av/Tv/ISO とも 8 = 1EV の線形構造
  (Av: 0x08=f/1 → 0x10=f/1.4、Tv: 0x38=1s → 0x40=1/2s、ISO: 0x48=100 → 0x50=200)

## 方針

- **strict をデフォルト**とし、最近傍への丸めは `nearest=True` のオプトイン
- 値は **数値(F値・秒・ISO値)/文字列("1/125" 等)/EDSDK生コード** の3形式をサポート
- 数値は常に写真的な値として解釈し、生コードは `from_code` 経由のみ(曖昧さ排除)
- 既存 API(特に `set_properties`)はスーパーセット互換を維持

## 設計

### 1. 新モジュール `edsdk/exposure.py`

既存の表示文字列テーブルからインポート時に数値テーブルを一度だけ構築し、
3つの frozen dataclass を提供する:

```python
@dataclass(frozen=True)
class Aperture:
    code: int        # EDSDK コード (例 0x30)
    f_number: float  # 5.6
    display: str     # "5.6"

@dataclass(frozen=True)
class ShutterSpeed:
    code: int
    seconds: float | None   # Bulb は None
    display: str            # '1/125', '0"5', 'Bulb'
    # property: is_bulb

@dataclass(frozen=True)
class ISOSpeed:
    code: int
    value: int | None       # Auto は None
    display: str            # "100", "Auto"
    # property: is_auto
```

生成メソッド:

- `from_code(code)` — 生コードから。未知コードは `ValueError`
- `from_any(value)` — 数値・文字列・同型インスタンスを受理
  - 数値: Aperture=F値 / ShutterSpeed=秒 / ISOSpeed=ISO値(0 は Auto)
  - 文字列: `"f/5.6"`, `"5.6"`, `"1/125"`, `'0"5'`, `"0.5s"`, `"bulb"`, `"ISO100"`, `"auto"` など
    現行 `_parse_av`/`_parse_tv`/`_parse_iso` の受理形式のスーパーセット
  - 同値異コードが複数マッチする場合(例: F3.5 → 0x24/0x25)、`from_any` 単体では
    **コード値が小さい方(標準ステップ側)** を返す。カメラ対応値を考慮した選択は
    `resolve_*`(下記)が候補集合を使って行う
- `0xFFFFFFFF`(NotValid)はテーブルから除外

### 2. 解決ロジック(カメラ対応値を考慮した値→コード解決)

`resolve_av / resolve_tv / resolve_iso(value, supported_codes, *, nearest=False)`:

1. 入力をテーブル候補コード群に変換。数値なら許容誤差内の**全候補**
   (例: 3.5 → {0x24, 0x25}。同値異コード問題への対応)
2. `supported_codes`(`GetPropertyDesc` 由来)に含まれる候補を優先して採用
3. どの候補も非対応の場合:
   - `nearest=False`: 分かりやすい `ValueError`(下記フォーマット)
   - `nearest=True`: EV 空間(log2)での最近傍サポート値に丸める。
     Bulb / ISO Auto は数値を持たないため最近傍対象から除外
4. `supported_codes` が空(記述子が取れないカメラ)の場合はテーブル全体を対象とする

### 3. CameraController の新メソッド

```python
ctrl.set_av(5.6)                    # -> Aperture(設定後に読み返した実際の値)
ctrl.set_tv("1/125", nearest=True)  # -> ShutterSpeed
ctrl.set_iso(100)                   # -> ISOSpeed
ctrl.get_av() / get_tv() / get_iso()
ctrl.supported_av()  -> list[Aperture]     # カメラ+レンズの実対応値
ctrl.supported_tv()  -> list[ShutterSpeed]
ctrl.supported_iso() -> list[ISOSpeed]
```

`set_*` は設定後にカメラから読み返した値を返す
(AE モードにより設定が無視されるケースの検出にもなる)。

### 4. 既存 API の互換性(置き換えマップ)

| 既存実装 | 扱い |
|---|---|
| `_parse_av` / `_parse_tv` / `_parse_iso` | 新モジュールに置き換え(削除) |
| `_tv_display_to_seconds` | 新モジュールに集約(削除) |
| `_reverse_lookup` | 新モジュールに置き換え(削除) |
| `list_supported()` | 戻り形式(文字列リスト)不変、内部を新実装に委譲 |
| `set_properties(validate=True)` | 維持。`nearest: bool = False` を追加し内部パースを委譲 |
| `save_profile` / `load_profile` | 不変。`get_properties()` の全表示文字列が `from_any` で往復可能であることをテストで保証 |

### 5. エラーメッセージ

```
ValueError: Av f/2.8 is not supported by this camera/lens.
  Supported range: f/3.5 - f/22 (17 values)
  Nearest supported: f/3.5
  Hint: pass nearest=True to snap to the nearest supported value.
```

## 外部利用者との互換性(Densoten)

`maedayoshiaki/Densoten` は本ライブラリの主要な下流利用者。確認した接点は
`src/procam/shared/camera/canon_edsdk.py` の1箇所のみ:

```python
camera.set_properties(av=cfg.av, tv=cfg.tv, iso=cfg.iso, image_quality=cfg.image_quality)
```

- Config(TOML)値は全て文字列: `av="5"`/`"8"`, `tv="1/15"`/`"1/30"`, `iso="400"`/`"500"`
- 依存は wheel ファイル名固定(vendor 同梱)のため、更新は明示的に行われる

互換性保証(実装要件に昇格):

1. `set_properties` のシグネチャは追加のみ(`nearest` キーワード専用引数)
2. 現行パーサーが受理する全文字列形式を新パーサーも受理する(スーパーセット互換)。
   特に `"5"` → テーブル表記 `"5.0"`(0x2D)への数値マッチを明示的にテスト
3. 非対応値の例外型は `ValueError` のまま(strict デフォルト。Config 値が黙って変わることはない)

## テスト計画

- `tests/test_exposure.py`(カメラ不要):
  - 表示文字列→数値変換(Av/Tv/ISO 全テーブルエントリの往復)
  - `from_any` の全形式(数値・各種文字列・インスタンス・不正値)
  - 解決ロジック: strict / nearest / 同値異コード優先 / Bulb・Auto 除外 / 空サポートリスト
  - 互換性テスト: 旧 `_parse_*` の受理形式一覧(Densoten の Config 値を含む)
- 実機テスト(EOS M6 Mark II):
  - `supported_av/tv/iso` の取得
  - `set_av(7.1)` 等の設定と読み返し
  - 範囲外 f/2.8 → strict でエラー / `nearest=True` で f/3.5 に丸め
  - `set_properties(av="5", tv="1/30", iso="500")`(Densoten 相当の呼び出し)の動作確認

## スコープ外

- ExposureCompensation ほか他プロパティへの同パターン適用(将来の拡張余地はあるが今回はやらない)
- AE モードの自動切り替え(Tv 設定時に M/Tv モードでない場合など)。エラーメッセージのヒント表示のみ検討
