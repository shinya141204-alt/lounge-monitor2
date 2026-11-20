"""
scheduled_articles.py - Time-gated (scheduled) editorial column publishing & X auto-release manager.

Articles registered here remain invisible to public visitors, `/columns`, `/`, `/sitemap.xml`,
and Google crawlers (returning HTTP 404 to non-staff) until `publish_at` (JST) arrives.
Logged-in staff (`/staff`) can preview upcoming scheduled articles at any time.
Once `publish_at` arrives:
  1. `/guide/<slug>` automatically goes live (HTTP 200).
  2. `/columns`, `/`, and `/sitemap.xml` automatically list the article.
  3. APScheduler automatically posts a 2-tweet release announcement thread to X (once per slug via `job_state`).
"""

import datetime
import logging

logger = logging.getLogger(__name__)

JST = datetime.timezone(datetime.timedelta(hours=9))

# Ordered by publish_at ascending (get_published_articles returns newest-published first)
SCHEDULED_ARTICLES = [
    {
        "slug": "local-vs-urban",
        "path": "/guide/local-vs-urban",
        "template": "guide_local_vs_urban.html",
        "publish_at": "2026-10-28 19:00:00",
        "publish_at_ja": "2026年10月28日(水) 19:00",
        "lastmod": "2026-10-28",
        "eyecatch_jpg": "/static/eyecatch-local-vs-urban.jpg",
        "eyecatch_webp": "/static/eyecatch-local-vs-urban.webp",
        "badge": "地方vs都心データ",
        "read_time": "約6分で読める",
        "tag_primary": ("tag-cyan", "都心33店×地方18店ログ比較"),
        "tag_secondary": ("tag-pink", "深夜0時の逆転現象"),
        "title": "地方店と都心の大型店、客層とピークはどう違うのか｜深夜0時に女性が121%へ増える地方の一極集中",
        "short_title": "地方店と都心大型店の客層・深夜ピーク比較",
        "desc": "「出張先で20時に行ったら空いていたのに、居酒屋帰りの23時半には満席近かった」。都心33店とは逆の動きを見せる地方18店の深夜121%の伸びと、現地で自然に会話が弾むコツ。",
        "x_title": "地方店と都心大型店｜深夜0時に女性が121%へ増える理由",
        "x_main_text": (
            "東京の感覚で出張先の相席ラウンジに20時に行ったら空いていて、居酒屋終わりの23時半に覗いたら女性グループで満席近かった。\n\n"
            "地方18店は終電の壁が薄く、深夜0〜2時に女性が121.1%まで増える。\n\n"
            "地元の人の過ごし方と狙い目はリプ欄で。\n"
            "#相席ラウンジ #相席ナビ"
        ),
    },
    {
        "slug": "osaka-kita-vs-minami",
        "path": "/guide/osaka-kita-vs-minami",
        "template": "guide_osaka_kita_vs_minami.html",
        "publish_at": "2026-10-31 19:00:00",
        "publish_at_ja": "2026年10月31日(土) 19:00",
        "lastmod": "2026-10-31",
        "eyecatch_jpg": "/static/eyecatch-osaka-kita-vs-minami.jpg",
        "eyecatch_webp": "/static/eyecatch-osaka-kita-vs-minami.webp",
        "badge": "大阪9店舗データ",
        "read_time": "約6分で読める",
        "tag_primary": ("tag-amber", "大阪全9店舗・27万件ログ"),
        "tag_secondary": ("tag-cyan", "キタvsミナミ南北ハシゴ"),
        "title": "大阪の相席ラウンジ・相席屋おすすめ比較｜梅田・難波・心斎橋・天満の全店男女比データ",
        "short_title": "大阪キタvsミナミ｜梅田・難波・心斎橋・天満の比較",
        "desc": "大阪（梅田・茶屋町・天満・難波・心斎橋）で相席ラウンジや相席屋・相席バーを探す方へ。27万件の実測ログから、20時の茶屋町（女子率86%）、22時の天満・大阪駅前、24時以降のJIS（ジス）梅田と難波へ主役が入れ替わる南北ハシゴルートを比較。",
        "x_title": "梅田キタと難波ミナミ｜大阪全9店舗の南北ハシゴ",
        "x_main_text": (
            "梅田の1軒目で「45分待ち」と言われた夜。東通りで粘るか、御堂筋線で9分南へ降りるかで、その後の流れがまるで変わる。\n\n"
            "20時は茶屋町（女子率86%）、深夜0時以降はJIS梅田と難波に主役が移る。\n\n"
            "全9店の南北の回り方はリプ欄にまとめました。\n"
            "#相席ラウンジ #相席ナビ"
        ),
    },
    {
        "slug": "cost-break-even",
        "path": "/guide/cost-break-even",
        "template": "guide_cost_break_even.html",
        "publish_at": "2026-11-01 12:30:00",
        "publish_at_ja": "2026年11月1日(日) 12:30",
        "lastmod": "2026-11-01",
        "eyecatch_jpg": "/static/eyecatch-cost-break-even.jpg",
        "eyecatch_webp": "/static/eyecatch-cost-break-even.webp",
        "badge": "料金×損益分岐点",
        "read_time": "約6分で読める",
        "tag_primary": ("tag-amber", "滞在時間×コスパ検証"),
        "tag_secondary": ("tag-cyan", "40分見切りの法則"),
        "title": "微妙な席で90分粘るか、40分で切り上げるか｜滞在時間とトータル料金の損益分岐点",
        "short_title": "滞在時間とトータル料金の損益分岐点",
        "desc": "「気を遣って90分延長したのに、女性側も化粧室のLINEで『いつ帰る？』と時計を見ていた」。お財布も空気もすり減らす惰性の滞在を、40分で区切るための損益分岐点。",
        "x_title": "90分粘るか、40分で切り上げるか｜相席ラウンジ料金の損益分岐点",
        "x_main_text": (
            "少し言いにくい話をすると、噛み合わない席で「悪い気もするし」と90分延長しても、向かいの女性も内心いつ出ようか時計を見ていることが多い。\n\n"
            "1万近いレシートを見て改札で後悔する前に、40分で区切る目安をリプ欄に。\n"
            "#相席ラウンジ #相席ナビ"
        ),
    },
    {
        "slug": "table-change-protocol",
        "path": "/guide/table-change-protocol",
        "template": "guide_table_change_protocol.html",
        "publish_at": "2026-11-03 19:00:00",
        "publish_at_ja": "2026年11月3日(火・祝) 19:00",
        "lastmod": "2026-11-03",
        "eyecatch_jpg": "/static/eyecatch-table-change-protocol.jpg",
        "eyecatch_webp": "/static/eyecatch-table-change-protocol.webp",
        "badge": "席替え・チェンジ検証",
        "read_time": "約6分で読める",
        "tag_primary": ("tag-pink", "27.6万件ログ実測"),
        "tag_secondary": ("tag-cyan", "スマート離脱プロトコル"),
        "title": "相席ラウンジの席替え（チェンジ）｜言い出せない男性と察している女性、27万件ログで見る成功店舗の条件",
        "short_title": "気まずい沈黙を終わらせる席替え（チェンジ）の頼み方",
        "desc": "「会話が盛り上がらないのに席替えを頼めず、10分750円のメーターだけが回る沈黙」。女性が化粧室で相談を始めるサイン、スタッフへのさりげない3つの伝え方、そして店内に女性が10名以上いないと席替えがうまくいかないデータ上の理由。",
        "x_title": "相席ラウンジの席替え｜言い出せない男性と察している女性の心理",
        "x_main_text": (
            "会話が止まったテーブルで「席替えを頼んだら失礼かな」と迷う間にも、10分750円のメーターは回っている。\n\n"
            "女性10名未満の店で頼むと、男性1人で待つ時間に料金だけがかかることも。\n\n"
            "お互いの気持ちを損なわずに席を離れる方法はリプ欄へ。\n"
            "#相席ラウンジ #相席ナビ"
        ),
    },
    {
        "slug": "thirties-lounge-guide",
        "path": "/guide/thirties-lounge-guide",
        "template": "guide_thirties_lounge_guide.html",
        "publish_at": "2026-11-04 19:00:00",
        "publish_at_ja": "2026年11月4日(水) 19:00",
        "lastmod": "2026-11-04",
        "eyecatch_jpg": "/static/eyecatch-thirties-lounge-guide.jpg",
        "eyecatch_webp": "/static/eyecatch-thirties-lounge-guide.webp",
        "badge": "30代・年齢層データ",
        "read_time": "約6分で読める",
        "tag_primary": ("tag-amber", "27.6万件客層ログ"),
        "tag_secondary": ("tag-cyan", "アラサーの店舗選び"),
        "title": "相席ラウンジは30代でも浮かない？｜学生ノリの店と、落ち着いた大人が集まる店の境界線",
        "short_title": "30代・アラサーのための相席ラウンジの選び方",
        "desc": "「20代前半ばかりで場違いでは」とためらうアラサー男女へ。全国27.6万件のログから、ag・オリエンタルラウンジ・JISの年齢層の違いと、平日20時に社会人が集まる時間帯を読み解く。",
        "x_title": "相席ラウンジは30代でも浮かない？｜27.6万件ログで見る店選びの境界線",
        "x_main_text": (
            "「20代前半ばかりで浮くんじゃないか」とためらっている30代の人へ。\n\n"
            "agは20代前半が7割ほどだけれど、オリエンタルラウンジやJISは社会人が6割を超える。\n\n"
            "水木20時の社会人が集まる時間帯と店選びはリプ欄に続きます。\n"
            "#相席ラウンジ #相席ナビ"
        ),
    },
    {
        "slug": "rain-payday",
        "path": "/guide/rain-payday",
        "template": "guide_rain_payday.html",
        "publish_at": "2026-11-05 19:00:00",
        "publish_at_ja": "2026年11月5日(木) 19:00",
        "lastmod": "2026-11-05",
        "eyecatch_jpg": "/static/eyecatch-rain-payday.jpg",
        "eyecatch_webp": "/static/eyecatch-rain-payday.webp",
        "badge": "天候・給料日データ",
        "read_time": "約5分で読める",
        "tag_primary": ("tag-pink", "天候・カレンダー検証"),
        "tag_secondary": ("tag-cyan", "給料日前の穴場法則"),
        "title": "雨の日・給料日前後・連休中日で男女比はどう動く？9.6万件ログで見えたライバルの少ない日",
        "short_title": "雨の日・給料日前後・連休中日の客足データ",
        "desc": "25日の給料日直後はスーツ姿の男性待ちが並ぶ一方、15〜24日の給料日前や、「前髪を守りたい」女性が集まる雨の日の地下直結店は意外と落ち着いて座れる。その理由を9.6万件ログから読み解く。",
        "x_title": "雨の日・給料日前・連休中日｜9.6万件ログで見る穴場日の傾向",
        "x_main_text": (
            "25日の給料日直後は男性の待ち列ができているのに、15〜24日の給料日前はライバルが減り、女性比は62.1%（+7.4pt）まで上がる。\n\n"
            "雨の日に前髪を濡らしたくない人が集まる地下直結の店など、穴場の日の話はリプ欄で。\n"
            "#相席ラウンジ #相席ナビ"
        ),
    },
    {
        "slug": "sunday-lounge-strategy",
        "path": "/guide/sunday-lounge-strategy",
        "template": "guide_sunday_lounge_strategy.html",
        "publish_at": "2026-11-07 19:00:00",
        "publish_at_ja": "2026年11月7日(土) 19:00",
        "lastmod": "2026-11-07",
        "eyecatch_jpg": "/static/eyecatch-sunday-lounge-strategy.jpg",
        "eyecatch_webp": "/static/eyecatch-sunday-lounge-strategy.webp",
        "badge": "日曜日・客層データ",
        "read_time": "約6分で読める",
        "tag_primary": ("tag-pink", "28.6万件ログ実測"),
        "tag_secondary": ("tag-cyan", "サンデーサク飲み検証"),
        "title": "日曜日の相席ラウンジは行くべきか？｜21時半を境に入れ替わる会社員と月曜休み組の実測データ",
        "short_title": "日曜日の相席ラウンジの狙い目と客層データ",
        "desc": "日曜日の相席ラウンジは土曜の約半分の人数ながら女性比率は61.2%と高水準。18:30〜21:00の女性先行サク飲み帯と21時半の客層入れ替わり、そして深夜に合流する月曜休み組（美容・アパレル・看護・シフト制）のリアルを28.6万件ログで解明。",
        "x_title": "日曜日の相席ラウンジは行くべきか？｜21時半の撤退急落と、月曜休みの隠れ狙い目",
        "x_main_text": (
            "日曜夜の相席ラウンジは土曜の3割の人数だけど、女性比率は61.2%と高い。\n\n"
            "ただ「21時半」に翌朝仕事のOLが一斉に帰るため、金土の感覚で遅く入ると男性余りに。\n\n"
            "18時台サク飲みか、22時以降の月曜休み組（美容・アパレル）狙いかの二択。\n\n"
            "データはリプ欄へ。\n"
            "#相席ラウンジ #相席ナビ"
        ),
    },
    {
        "slug": "line-exchange-reality",
        "path": "/guide/line-exchange-reality",
        "template": "guide_line_exchange_reality.html",
        "publish_at": "2026-11-11 19:00:00",
        "publish_at_ja": "2026年11月11日(水) 19:00",
        "lastmod": "2026-11-11",
        "eyecatch_jpg": "/static/eyecatch-line-exchange-reality.jpg",
        "eyecatch_webp": "/static/eyecatch-line-exchange-reality.webp",
        "badge": "LINE交換・実測検証",
        "read_time": "約6分で読める",
        "tag_primary": ("tag-pink", "10.6万件ログ実測"),
        "tag_secondary": ("tag-cyan", "連絡先交換プロトコル"),
        "title": "相席ラウンジのLINE交換、なぜ翌日既読スルーされるのか？｜1晩で3組と話す女性の記憶容量と初デートに繋がる連絡の境界線",
        "short_title": "相席ラウンジのLINE交換と翌日既読スルーの境界線",
        "desc": "「席ではあんなに盛り上がってLINE交換したのに、翌日お礼を送ったら既読スルー…」。1晩で平均3組（計6名）と話す女性の記憶忘却曲線と、グループLINEの罠、そして初デートに繋がる退店30分の連絡プロトコルを実測検証。",
        "x_title": "相席ラウンジのLINE交換、なぜ翌日既読スルーされるのか？｜女性の記憶容量と連絡の境界線",
        "x_main_text": (
            "席では笑顔でLINE交換したのに、翌日お礼を送ったら既読スルーされる理由。\n\n"
            "男性は1組しか話していなくても、女性は1晩で平均3組（計6名）と会話しています。\n\n"
            "お酒も入った翌朝の記憶容量と、退店30分以内に差がつく連絡プロトコルをまとめました。\n"
            "#相席ラウンジ #相席ナビ"
        ),
    },
    {
        "slug": "free-food-reality",
        "path": "/guide/free-food-reality",
        "template": "guide_free_food_reality.html",
        "publish_at": "2026-11-14 19:00:00",
        "publish_at_ja": "2026年11月14日(土) 19:00",
        "lastmod": "2026-11-14",
        "eyecatch_jpg": "/static/eyecatch-free-food-reality.jpg",
        "eyecatch_webp": "/static/eyecatch-free-food-reality.webp",
        "badge": "フード×男女比データ",
        "read_time": "約6分で読める",
        "tag_primary": ("tag-pink", "9.6万件ログ実測"),
        "tag_secondary": ("tag-cyan", "19時台と21時台の波形"),
        "title": "相席ラウンジの女性は『ご飯目的（タダ飯）』なのか？｜19時台と21時台で店が別物になる、9.6万件ログの男女比波形",
        "short_title": "相席ラウンジの女性はご飯目的なのか？実測検証",
        "desc": "「10分750円払っているのに、向かいは食べてばかり」。全国9.6万件の実測ログから、19時台の女性比率80.9%という先行ディナー帯と22時台の男性8倍急増による出会いモードの真実、オリエンタルラウンジが本格料理を女性無料にする集客構造、そして席での男女の心理を検証。",
        "x_title": "相席ラウンジの女性はご飯目的なのか？｜9.6万件ログで見えた本音の境界線",
        "x_main_text": (
            "「男は10分750円払ってるのに女はタダ飯？」と疑ってしまう人へ。\n\n"
            "9.6万件ログで見ると、19時台は女性比率80.9%の先行ディナー帯ですが、22時台には男性が8倍以上に急増して本格相席へ切り替わります。\n\n"
            "女性の沈黙の逃げ場フードと会話のコツはリプ欄へ。\n"
            "#相席ラウンジ #相席ナビ"
        ),
    },
    {
        "slug": "dome-live-after-surge",
        "path": "/guide/dome-live-after-surge",
        "template": "guide_dome_live_after_surge.html",
        "publish_at": "2026-11-18 19:00:00",
        "publish_at_ja": "2026年11月18日(水) 19:00",
        "lastmod": "2026-11-18",
        "eyecatch_jpg": "/static/eyecatch-dome-live-after-surge.jpg",
        "eyecatch_webp": "/static/eyecatch-dome-live-after-surge.webp",
        "badge": "大型公演×ターミナル波形",
        "read_time": "約6分で読める",
        "tag_primary": ("tag-cyan", "5大ドーム導線実測"),
        "tag_secondary": ("tag-pink", "終演60〜90分後の第2波"),
        "title": "ドーム・アリーナ終演後の相席ラウンジ急増波｜21時梅田・新宿・天神・札幌で起きる「参戦帰り第2波」実測データ検証",
        "short_title": "ドーム・アリーナ終演後の相席ラウンジ急増波",
        "desc": "京セラドーム大阪・東京ドーム・みずほPayPayドーム・大和ハウス プレミストドーム・バンテリンドームの終演60〜90分後に、主要ターミナルの相席ラウンジで何が起きるのか。直近30日11.6万時間サンプルの実測データから検証。",
        "x_title": "ドーム・アリーナ終演後の相席ラウンジ急増波｜参戦帰り第2波の実測検証",
        "x_main_text": (
            "5万人規模のドーム公演が終わった夜、梅田・新宿・天神・すすきのの相席ラウンジで何が起きるか。\n\n"
            "規制退場＋地下鉄移動を経て、終演60〜90分後にターミナル旗艦店の女性人数が一段跳ね上がります。\n\n"
            "11.6万時間サンプルで見えた男女別の狙い目と受付待ち回避法はリプ欄へ。\n"
            "#相席ラウンジ #相席ナビ"
        ),
    },
    {
        "slug": "lounge-vs-aisekiya-bar",
        "path": "/guide/lounge-vs-aisekiya-bar",
        "template": "guide_lounge_vs_aisekiya_bar.html",
        "publish_at": "2026-11-18 19:00:00",
        "publish_at_ja": "2026年11月18日(水) 19:00",
        "lastmod": "2026-11-20",
        "eyecatch_jpg": "/static/eyecatch-lounge-vs-aisekiya-bar.jpg",
        "eyecatch_webp": "/static/eyecatch-lounge-vs-aisekiya-bar.webp",
        "badge": "4業態実測比較",
        "read_time": "約6分で読める",
        "tag_primary": ("tag-pink", "11.5万時間データ検証"),
        "tag_secondary": ("tag-cyan", "相席ラウンジ・相席屋・相席バー"),
        "title": "相席ラウンジとは？相席屋・THE SINGLE・相席バー（立ち飲み）との違いを実測比較｜女性比61%と39%の境界線",
        "short_title": "相席ラウンジとは？相席屋・1対1・相席バー4業態の実測比較",
        "desc": "相席ラウンジとは何か、居酒屋型の相席屋・1対1専門のTHE SINGLE・立ち飲み相席バー（ASOBIBAR・パブリックスタンド）とどう違うのか。11.5万時間ログから、女性比率が61〜64%と39.0%に分かれる構造を比較検証。",
        "x_title": "相席ラウンジとは？相席屋・THE SINGLE・相席バー4業態の実測比較",
        "x_main_text": (
            "同じ金曜夜の渋谷・新宿・梅田なのに、着席の相席ラウンジは女性比61〜64%、立ち飲みバー（ASOBIBAR）は女性比39.0%と20pt以上の差がつく。\n\n"
            "理由は「10分従量課金か定額無制限か」という滞在時間の構造。\n\n"
            "相席屋・THE SINGLEも含めた4業態の使い分けはリプ欄へ。\n"
            "#相席ラウンジ #相席ナビ"
        ),
    },
    {
        "slug": "app-fatigue-vs-lounge",
        "path": "/guide/app-fatigue-vs-lounge",
        "template": "guide_app_fatigue_vs_lounge.html",
        "publish_at": "2026-11-21 19:00:00",
        "publish_at_ja": "2026年11月21日(土) 19:00",
        "lastmod": "2026-11-21",
        "eyecatch_jpg": "/static/eyecatch-app-fatigue-vs-lounge.jpg",
        "eyecatch_webp": "/static/eyecatch-app-fatigue-vs-lounge.webp",
        "badge": "出会い経路×時間コスト",
        "read_time": "約6分で読める",
        "tag_primary": ("tag-amber", "メッセージ往復vs対面0秒"),
        "tag_secondary": ("tag-cyan", "25.7万件ログ検証"),
        "title": "「アプリ疲れ」の夜になぜ相席ラウンジへ人が戻るのか｜メッセージ往復2週間 vs 着席0秒のリアル検証",
        "short_title": "「アプリ疲れ」の夜に相席ラウンジへ人が戻る理由",
        "desc": "こども家庭庁の調査で結婚のきっかけ首位（25.1%）となったマッチングアプリの裏で広がるメッセージ疲れ。累計25.7万件の混雑ログから、2週間の文面往復と乾杯0秒の対面会話の使い分けを検証。",
        "x_title": "「アプリ疲れ」の夜になぜ相席ラウンジへ人が戻るのか｜実測ログ検証",
        "x_main_text": (
            "2週間メッセージを重ねて会った最初の3秒で「写真の空気感と違うかも」と悟る疲れ。\n\n"
            "アプリ婚が25.1%を占める一方で、対面の相席ラウンジに社会人が戻り続ける構造的な理由を25.7万件ログから整理しました。\n\n"
            "疲れの種類別の使い分けはリプ欄へ。\n"
            "#相席ラウンジ #相席ナビ"
        ),
    },
    {
        "slug": "non-alcohol-lounge-guide",
        "path": "/guide/non-alcohol-lounge-guide",
        "template": "guide_non_alcohol_lounge_guide.html",
        "publish_at": "2026-11-21 19:00:00",
        "publish_at_ja": "2026年11月21日(土) 19:00",
        "lastmod": "2026-11-21",
        "eyecatch_jpg": "/static/eyecatch-non-alcohol-lounge-guide.jpg",
        "eyecatch_webp": "/static/eyecatch-non-alcohol-lounge-guide.webp",
        "badge": "ノンアル・下戸×時間帯",
        "read_time": "約5分で読める",
        "tag_primary": ("tag-pink", "30日時間帯ログ検証"),
        "tag_secondary": ("tag-cyan", "19時台79.6% vs 23時台"),
        "title": "お酒が飲めない人は、相席ラウンジで浮くのか｜19時台と23時台の人数ログで確かめた",
        "short_title": "お酒が飲めない人は相席ラウンジで浮くのか？",
        "desc": "飲み放題の店で自分だけウーロン茶。それで浮くかどうかは、何時に入るかで変わります。直近30日の人数ログでは19時台は女性12.2名・男性2.9名、23時台は男性17.3名。飲めない男女それぞれの入りやすい時間、切り上げどき、ブランドの選び方をまとめました。",
        "x_title": "お酒が飲めない人は、相席ラウンジで浮くのか｜19時台と23時台の人数ログで確かめた",
        "x_main_text": (
            "飲み放題の店で、自分だけウーロン茶。相席ラウンジでお酒が飲めない人は浮くのか。\n\n"
            "直近30日のログでは、19時台は女性12.2名に男性2.9名（女性比79.6%）。23時台には男性が17.3名まで増えます。\n\n"
            "飲めない男女それぞれの入りやすい時間帯と店選びはリプ欄に。\n"
            "#相席ラウンジ #相席ナビ"
        ),
    },
]


def now_jst_naive() -> datetime.datetime:
    return datetime.datetime.now(JST).replace(tzinfo=None)


def _coerce_naive_jst(now_jst: datetime.datetime = None) -> datetime.datetime:
    if now_jst is None:
        now_jst = now_jst_naive()
    if now_jst is not None and now_jst.tzinfo is not None:
        now_jst = now_jst.astimezone(JST).replace(tzinfo=None)
    return now_jst


def parse_publish_at(publish_at_str: str) -> datetime.datetime:
    raw = str(publish_at_str or "").strip()
    s = raw.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return _coerce_naive_jst(dt)
    except Exception:
        logger.warning(f"[scheduled_articles] Invalid publish_at '{publish_at_str}'; failing closed.")
        return datetime.datetime(9999, 12, 31, 23, 59, 59)


def get_all_scheduled_articles(allow_blocking: bool = False) -> list:
    """Combine built-in scheduled articles with dynamically published/scheduled editorial columns."""
    combined = list(SCHEDULED_ARTICLES)
    seen = {item["slug"] for item in combined}
    try:
        import column_editorial
        for dyn in column_editorial.get_dynamic_scheduled_articles(allow_blocking=allow_blocking):
            sl = dyn.get("slug")
            if sl and sl not in seen:
                seen.add(sl)
                combined.append(dyn)
    except Exception as e:
        logger.warning(f"[scheduled_articles] Dynamic column load skipped: {e}")
    return combined


def get_article_by_slug(slug: str, allow_blocking: bool = True) -> dict:
    slug_clean = (slug or "").strip("/")
    if not slug_clean:
        return None
    for item in get_all_scheduled_articles(allow_blocking=False):
        if item["slug"] == slug_clean:
            return item
    if not allow_blocking:
        return None
    import job_state
    if getattr(job_state, "_sheets_loaded", False) or job_state.sheets_store.is_disabled():
        return None
    for item in get_all_scheduled_articles(allow_blocking=True):
        if item["slug"] == slug_clean:
            return item
    return None


def is_published(slug_or_item, now_jst: datetime.datetime = None) -> bool:
    now_jst = _coerce_naive_jst(now_jst)
    item = slug_or_item if isinstance(slug_or_item, dict) else get_article_by_slug(str(slug_or_item))
    if not item:
        return True  # Non-scheduled legacy articles are already public
    return now_jst >= parse_publish_at(item.get("publish_at"))


def get_published_articles(now_jst: datetime.datetime = None, allow_blocking: bool = False) -> list:
    """Return scheduled articles whose publish_at has arrived, sorted newest first."""
    now_jst = _coerce_naive_jst(now_jst)
    published = [item for item in get_all_scheduled_articles(allow_blocking=allow_blocking) if is_published(item, now_jst=now_jst)]
    # Sort by parsed datetime (mixed "YYYY-MM-DD HH:MM" / ISO "T" / "+09:00" formats sort wrongly as strings)
    published.sort(key=lambda x: parse_publish_at(x.get("publish_at")), reverse=True)
    return published


def get_upcoming_articles(now_jst: datetime.datetime = None, allow_blocking: bool = False) -> list:
    """Return scheduled articles waiting for release, sorted soonest first."""
    now_jst = _coerce_naive_jst(now_jst)
    upcoming = [item for item in get_all_scheduled_articles(allow_blocking=allow_blocking) if not is_published(item, now_jst=now_jst)]
    upcoming.sort(key=lambda x: parse_publish_at(x.get("publish_at")))
    return upcoming


def get_all_with_status(now_jst: datetime.datetime = None, allow_blocking: bool = False) -> list:
    """Return all scheduled articles with live `published` boolean flag for `/staff` dashboard."""
    now_jst = _coerce_naive_jst(now_jst)
    out = []
    for item in sorted(get_all_scheduled_articles(allow_blocking=allow_blocking), key=lambda x: parse_publish_at(x.get("publish_at"))):
        copy_item = dict(item)
        copy_item["published"] = is_published(item, now_jst=now_jst)
        out.append(copy_item)
    return out


def check_and_announce_newly_published(app_url: str = "", now_jst: datetime.datetime = None) -> list:
    """
    Called periodically by APScheduler.
    If an article's `publish_at` has arrived (within the last 72h) and hasn't been announced on X yet,
    posts its 2-tweet release thread and records `col_release:<slug>` in `job_state`.
    """
    import os
    import job_state
    import x_poster

    now_jst = _coerce_naive_jst(now_jst)
    site_url = (app_url or os.environ.get("PUBLIC_SITE_URL") or "https://aiseki-navi.com").rstrip("/")
    results = []

    if not job_state.is_storage_healthy():
        reason = "Persistent job_state storage unhealthy; deferring column X release to prevent duplicate post"
        logger.warning(f"[scheduled_articles] {reason}")
        return [{"slug": "*", "result": {"status": "skipped", "reason": reason}}]

    for item in get_all_scheduled_articles(allow_blocking=True):
        pub_dt = parse_publish_at(item.get("publish_at", ""))
        if now_jst < pub_dt:
            continue
        # Safety window: only auto-announce within 72 hours of scheduled release
        age_hours = (now_jst - pub_dt).total_seconds() / 3600.0
        if age_hours > 72.0:
            continue

        dedup_key = f"col_release:{item['slug']}"
        if job_state.is_done(dedup_key):
            continue

        biz_date_str = (now_jst - datetime.timedelta(hours=7)).strftime("%Y-%m-%d")
        daily_col_key = f"col_release_daily:{biz_date_str}"
        if job_state.is_done(daily_col_key):
            reason = f"Daily column X release cap reached ({daily_col_key})"
            logger.info(f"[scheduled_articles] Deferring X announcement for {item['slug']} ({reason})")
            results.append({"slug": item["slug"], "result": {"status": "skipped", "reason": reason}})
            break

        mins_of_day = now_jst.hour * 60 + now_jst.minute
        if mins_of_day < (11 * 60 + 30) or mins_of_day > (23 * 60 + 25):
            reason = f"Holding column X announcement during quiet off-hours ({now_jst.strftime('%H:%M')} JST)"
            logger.info(f"[scheduled_articles] Deferring X announcement for {item['slug']} ({reason})")
            results.append({"slug": item["slug"], "result": {"status": "skipped", "reason": reason}})
            break

        if (19 * 60 + 42 <= mins_of_day <= 20 * 60 + 30) or (21 * 60 + 42 <= mins_of_day <= 22 * 60 + 30):
            reason = f"Holding column X announcement outside prime live slot rush ({now_jst.strftime('%H:%M')} JST)"
            logger.info(f"[scheduled_articles] Deferring X announcement for {item['slug']} ({reason})")
            results.append({"slug": item["slug"], "result": {"status": "skipped", "reason": reason}})
            break

        in_cd, cd_reason = x_poster.check_x_auto_post_cooldown(min_interval_minutes=48, now_jst=now_jst)
        if in_cd:
            logger.info(f"[scheduled_articles] Deferring X announcement for {item['slug']} ({cd_reason})")
            results.append({"slug": item["slug"], "result": {"status": "skipped", "reason": cd_reason}})
            break

        over_b, b_reason = x_poster.check_x_daily_post_budget(now_jst=now_jst, is_primary_peak=False)
        if over_b:
            logger.info(f"[scheduled_articles] Deferring X announcement for {item['slug']} ({b_reason})")
            results.append({"slug": item["slug"], "result": {"status": "skipped", "reason": b_reason}})
            break

        try:
            import turbo_store
            if not turbo_store.acquire_distributed_lock(f"col_rel_x:{item['slug']}", ttl_sec=120):
                reason = f"Distributed lock held by peer worker ({item['slug']})"
                results.append({"slug": item["slug"], "result": {"status": "skipped", "reason": reason}})
                continue
        except Exception:
            pass

        reply_text = (
            f"新しいコラムを書きました\n"
            f"『{item['x_title']}』\n"
            f"{site_url}{item['path']}?s=x_rel"
        )
        media_bytes, media_mime = x_poster.load_column_eyecatch_bytes(item)
        res = x_poster.post_to_x(
            item["x_main_text"],
            reply_text=reply_text,
            media_bytes=media_bytes,
            media_mime_type=media_mime,
        )
        if res.get("status") == "success":
            job_state.mark_done(dedup_key)
            job_state.mark_done(daily_col_key)
            job_state.mark_done(f"x_col_used:{now_jst.strftime('%Y-%m-%d')}:{item['path']}")
            x_poster.record_x_auto_post_timestamp(now_jst)
            try:
                import sns_editorial
                sns_editorial.record_recent_x_text(item["x_main_text"])
            except Exception:
                pass
            logger.info(f"[scheduled_articles] Announced newly released column on X: {item['slug']}")
        results.append({"slug": item["slug"], "result": res})
        if res.get("status") == "success":
            # At most 1 newly published column X announcement per 15-min check cycle to prevent burst firing
            break

    return results
