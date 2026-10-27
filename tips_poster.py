"""
tips_poster.py - Natural, Human-Tone Aiseki Lounge Tips & Realities for X (Twitter)

Posts relatable, grounded nightlife & lounge observations twice daily:
- 12:00 JST: Midday casual thoughts / talk tips / real atmosphere
- 18:00 JST: Evening pre-outing tips / grooming / timing & store selection
"""

import os
import datetime
import logging
import x_poster

logger = logging.getLogger(__name__)

# 60 relatable, human-toned tips written like an experienced regular sharing real-world observations.
# Each tip is sized so that the tip + live link + hashtags stays strictly within X API's 280-weight limit.
TIPS_LIST = [
    # --- 1. 会話・空気感 ---
    (
        "相席で最初の3分に「休みの日なにしてるの？」って聞くと、どうしても面接っぽくなりがち。\n"
        "最初は「今日仕事帰り？お疲れ！」「お腹空いてない？何か頼もうか」くらいから入る方がお互い肩の力が抜けて話しやすいよ🍻"
    ),
    (
        "相席で一番空気が重くなるのって、実は仕事の自慢話だったりする。\n"
        "女の子も仕事終わりに息抜きで来てるから、すごい話より「それめっちゃわかる笑」って笑える話の方が結果的に仲良くなれるんだよね。"
    ),
    (
        "初対面で話題に迷ったら「最近観たネトフリ」「最近食べて美味しかったもの」「次行きたい旅行先」のどれか振ってみて。\n"
        "この3つは誰でも答えやすいし、自然と「じゃあ今度そこ行こうよ」って流れになりやすい☕️"
    ),
    (
        "「何歳に見える？」って聞かれたとき、ただ若く言うだけだとちょっとわざとらしい。\n"
        "「落ち着いてるから同い年くらいかと思ったけど、笑うと年下っぽいね」くらいで返すと場が和むよ。"
    ),
    (
        "最初の乾杯のあと、2杯目を頼むタイミングでさりげなくお水（チェイサー）も一緒に頼んであげるとかなり喜ばれる。\n"
        "お酒強い子でも、こういうちょっとした気遣いができる人はやっぱり印象いいよね🥃"
    ),
    (
        "会話が続かないときって、無意識に一問一答の質問攻めになってることが多い。\n"
        "相手が答えたらすぐ次の質問に行くんじゃなくて、「いいなそれ、実は俺もさ〜」って自分のエピソードを少し混ぜると自然に会話が転がるよ。"
    ),
    (
        "「趣味なに？」って聞かれると意外と答えにくいけど、「最近なんかハマってることある？」って聞かれると急に話しやすくなる。\n"
        "ほんのちょっと聞き方を変えるだけで、相手のリアクションってだいぶ変わるよ😊"
    ),
    (
        "席に着いたとき、ネイルとかピアスとかスマホケースに気づいて「その色かわいいね」って自然に言える人はやっぱり強い。\n"
        "女の子も時間かけて準備してきてるから、細かいところに気づいてもらえると嬉しいもの💅"
    ),
    (
        "もし会話が途切れてちょっと気まずくなったら、無理に話題をひねり出すより「ここのメニューで何が一番人気か店員さんに聞いてみよっか」って巻き込むのがおすすめ。\n"
        "一回空気がリセットされて話しやすくなるよ🍕"
    ),
    (
        "自己紹介って、肩書きを真面目に話すより「〇〇関係の仕事してて、最近は仕事終わりにサウナかラーメン開拓ばっかりしてる笑」くらいの方が絶対話しかけやすい。\n"
        "ちょっと隙があるくらいがちょうどいい🍜"
    ),

    # --- 2. 身だしなみ・清潔感のリアル ---
    (
        "ラウンジの照明って暗いから油断しがちだけど、ソファ席だと距離が近いから肌のテカリと夕方の青ヒゲは想像以上に見られてる。\n"
        "入る前にトイレで軽く顔を拭いてリップ塗るだけでも、第一印象だいぶ変わるよ🪞"
    ),
    (
        "相席に行くとき、服には気を使っても靴が汚れてると結構もったいない。\n"
        "席に座るときや移動するとき、足元と手元（爪の長さ）って意外と見られてるから、出る前にサッと確認しておくのがおすすめ👟"
    ),
    (
        "相席のソファ席って距離が近いから、香水が強いとそれだけで敬遠されちゃうことがある。\n"
        "出かける30分前にワンプッシュだけにするか、柔軟剤とかヘアバームくらいのふわっと香る程度が一番ウケいいよ🫧"
    ),
    (
        "「相席に何着ていけばいいかわからない」って人は、無地の白Tやニットに紺か黒のジャケット羽織っておけばまず外さない。\n"
        "派手なハイブランドのロゴより、シワがなくてサイズ感が合ってる服の方がずっと好印象👔"
    ),
    (
        "暗い店内だと、前髪で目元や眉毛が隠れてるとちょっと暗い印象に見えがち。\n"
        "おでこを少し出すスタイルにして眉毛だけ整えておくと、パッと見の清潔感がぐっと上がるよ✂️"
    ),
    (
        "1軒目の居酒屋でニンニク料理を食べたりタバコを吸ったあとにそのまま相席に行くと、距離が近いぶん口臭で損しやすい。\n"
        "入る前にコンビニでブレスケアかマウスウォッシュ買っておくのが鉄板🦷"
    ),
    (
        "メニューを指差すときやグラスを持つとき、指先って女の子の目線に一番入る場所。\n"
        "爪が伸びっぱなしだとそれだけで恋愛対象から外れちゃうこともあるから、出かける前に短く切っておこう✋"
    ),
    (
        "テーブルの上に置いたスマホの画面がバキバキだったり指紋だらけだと、なんとなく生活感が出ちゃう。\n"
        "LINE交換するときにも絶対画面は見られるから、入る前にキレイにしておくとスマート📱"
    ),
    (
        "高い服を着ていく必要はぜんぜんないけど、ニットの毛玉とかTシャツの首元のヨレだけは要注意。\n"
        "ユニクロでもZARAでもいいから、ヨレてない小綺麗な服を着てる人の方が清潔感あってモテるよ👕"
    ),
    (
        "体型を隠そうとしてダボッとした大きめの服を着るより、肩幅と丈がちゃんと合ってるジャストサイズの服を選ぶ方がスッキリ見える。\n"
        "サイズ感だけで大人っぽさはかなり作れるよ✨"
    ),

    # --- 3. 2人組での動き方・ペアの空気 ---
    (
        "男2人で相席に行って対面席に座るときは、気になる子の正面じゃなくて「斜め向かい」に座るのがおすすめ。\n"
        "真正面だとお互いちょっと緊張するけど、斜めだと目線も外しやすくて自然に話せるよ👥"
    ),
    (
        "相席に一緒に行く友達選びって実はかなり大事。\n"
        "2人ともガンガン喋るタイプか、2人とも大人しいタイプだとバランスが難しい。片方が盛り上げて片方が聞き役に回れるペアが一番強い🤝"
    ),
    (
        "場を盛り上げようとして、一緒に来た友達を下げて笑いを取るのは逆効果になりがち。\n"
        "「こいつ仕事めっちゃ真面目なんだよ」「料理うまいんだよ」って自然に友達を褒められるペアは、2人とも好感度が上がる✨"
    ),
    (
        "トイレに立つタイミングは、相方とLINEで「今どう？」「そろそろ2軒目誘ってみる？」って温度感を合わせるチャンス。\n"
        "ここが噛み合ってると、お会計から外に出るまでの流れがすごく自然になる📲"
    ),
    (
        "4人で話してて、片方の女の子がちょっと静かにしてたら「〇〇ちゃんはどう？」ってさりげなく話を振ってあげて。\n"
        "1人だけ置いてけぼりにならないように気配りできる人は、女の子2人からの信頼が一気に上がるよ👏"
    ),
    (
        "もし相方がタイプの女の子と良い雰囲気で盛り上がってたら、もう1人の子と楽しく話す役に徹するのが一番のアシスト。\n"
        "どっちか1人が退屈しちゃうと「そろそろ帰ろっか」って流れになりやすいからね🔑"
    ),
    (
        "入店する前に、友達と「何時くらいまでいるか」とか「2軒目どこ行くか」だけざっくり話しておくと楽。\n"
        "その場で迷わないペアは、女の子から見ても余裕があってスマートに映るよ👔"
    ),
    (
        "男同士で盛り上がって、内輪ネタや昔の思い出話ばかりし始めると女の子はスマホを触り出しちゃう。\n"
        "「今日ここにいる4人全員がわかる話」をするのって、当たり前だけど一番大事なポイント🍻"
    ),

    # --- 4. 連絡先交換・2軒目の誘い方 ---
    (
        "LINEやインスタを聞くタイミングって、お会計や席替えの直前より「会話が一番盛り上がった瞬間」が自然。\n"
        "「さっき言ってたお店、あとでリンク送るね！」って流れなら断られることもほぼないよ☕️"
    ),
    (
        "2軒目に誘うとき、「もう1軒飲みに行こう」より「近くに夜パフェ（美味しいラーメン）あるからちょっと寄らない？」の方が圧倒的に来てもらいやすい。\n"
        "ハードルを少し下げてあげるのがコツ🍨"
    ),
    (
        "もしその日のうちに2軒目へ行きたいなら、22時半〜23時前には声をかけるのがおすすめ。\n"
        "23時半を過ぎると「終電あるから帰らなきゃ」ってなりやすいから、少し早めくらいがちょうどいい⏰"
    ),
    (
        "解散したあとの最初のLINEは、家に着いてからより「別れて5〜10分以内」の帰りの電車の中が一番返ってきやすい。\n"
        "「今日めっちゃ楽しかった！気をつけて帰ってね」くらいの短い一言がベスト💌"
    ),
    (
        "2軒目を断られたときに、ちょっとでも不機嫌になるのは一番もったいない。\n"
        "「明日仕事早いもんね、今日話せて楽しかった！またご飯行こう」って気持ちよく解散できる人は、後日のデートに繋がりやすいよ✨"
    ),
    (
        "LINEのアイコン、キメキメの自撮りより「友達に撮ってもらった自然な写真」か「美味しそうなご飯・旅行先の風景」にしておくのが無難。\n"
        "交換したあと、女の子はアイコンを意外とよく見てるよ📷"
    ),
    (
        "お店を出たあとに「どこ行こうか〜」って路上でスマホ見ながら迷うと、それだけでテンションが下がっちゃう。\n"
        "よく行くエリアなら、歩いて3分以内の静かなバーかカフェを1〜2軒だけ頭に入れておくと本当に強い🚶"
    ),
    (
        "最近はLINEより「インスタ交換しよ！」の方がハードル低くて自然なことも多い。\n"
        "お互いの普段の雰囲気もわかるし、ストーリーにちょっとリアクションするところから気軽に仲良くなれるよ📸"
    ),

    # --- 5. お店のシステム・上手な使い方 ---
    (
        "座ってみて「ちょっと話が合わないな」と思ったら、無理して粘らずに早めにお店を変えるか席替えをお願いする方がお互いのため。\n"
        "合わない席で気を使いながら1時間いるのが一番疲れちゃうからね💡"
    ),
    (
        "各ブランドの公式アプリ、行く前にダウンロードしておくだけでチャージ料が無料になったりポイントが貯まったりする。\n"
        "地味だけど毎回行ってると数千円単位で変わってくるから入れておいて損なし📱"
    ),
    (
        "オリエンタルラウンジとagとJIS、同じエリアにあっても雰囲気がぜんぜん違う。\n"
        "落ち着いて話したいならオリラジ、20代前半とワイワイ飲むならag、バーっぽい雰囲気ならJISって使い分けると失敗しにくいよ🍸"
    ),
    (
        "店員さんへの態度って、女の子はめちゃくちゃ見てる。\n"
        "ドリンク持ってきてもらったときに軽く「ありがとうございます」って言えるかどうかで、その人の普段の余裕が全部出るよね🤵‍♂️"
    ),
    (
        "新宿・渋谷・梅田みたいに近くに何店舗もあるエリアなら、1店舗に固執しなくて大丈夫。\n"
        "入ってきても待ち時間が長そうなら、すぐ近くの別ブランドに回る方が結果的に早く飲めることが多いよ👟"
    ),

    # --- 6. 曜日・時間帯・混雑のリアル ---
    (
        "金土の22時以降はやっぱり人が多くて盛り上がってるけど、そのぶん男性の入店待ちも出やすい。\n"
        "もし待ち時間なしでゆっくり話したいなら、水曜・木曜の20時〜21時半あたりが実は一番の穴場だったりする🍷"
    ),
    (
        "週末（金・土）に行くなら、21時を回る前の「19:30〜20:30」に入っておくのが一番スムーズ。\n"
        "混み始める前に席についておけば、入り口で待たされるストレスなく乾杯できるよ🏃‍♂️"
    ),
    (
        "日曜の夜って意外と知られてないけど、月曜休みの美容師さんやアパレル・医療系の子が来てて結構あたりが多い。\n"
        "店内も金土ほどガヤガヤしてないから、落ち着いて話したい人にはかなりおすすめ🌙"
    ),
    (
        "雨の日の夜って「今日は人少ないかな」と思いきや、男性の出足が鈍るぶん待ち時間がほぼなくて、地下街直結の店舗にはしっかり女性が集まってることが多い。\n"
        "雨の日は駅チカ店舗が狙い目☔️"
    ),
    (
        "終電後の0時半〜2時くらいは、残ってる人のテンションがはっきりしてる時間帯。\n"
        "男性の待ち列も解消されてすぐ入れることが多いから、2軒目・3次会でふらっと寄るのにも向いてるよ🔥"
    ),
    (
        "せっかくお店まで行ったのに「いま男性待ち1時間です」って言われるのが一番テンション下がるよね。\n"
        "向かう前にスマホで今の男女人数だけチラッと見ておくと、空振りがなくなって本当に楽になるよ📱"
    ),
    (
        "女性向けのキャンペーンやイベントをやってる日は、やっぱり普段より早い時間から女性が集まりやすい。\n"
        "行く店舗を迷ったら、その日に特典やフェアをやってる店舗から覗いてみるのがおすすめ🎯"
    ),
    (
        "出張や旅行で札幌・仙台・名古屋・福岡あたりに行ったとき、現地の相席ラウンジにふらっと寄ると「どこから来たんですか？」ってそれだけで話が盛り上がる。\n"
        "地元の美味しいお店を教えてもらう流れが鉄板✈️"
    ),
    (
        "同じ新宿や渋谷でも、日によって「こっちの店は男性待ちなのに、歩いて3分のあっちの店は女性の方が多い」っていう偏りが普通にある。\n"
        "看板だけで決めずに、その日の人数バランスを見て選ぶのが一番確実📊"
    ),

    # --- 7. メンタル・余裕 ---
    (
        "「せっかくお金払ってるんだから元を取らなきゃ」って気負いすぎると、どうしても表情が硬くなっちゃう。\n"
        "「美味しいお酒飲みながら楽しく話せたらラッキー」くらいの余裕がある人の方が、結果的に一番モテるんだよね🍷"
    ),
    (
        "もし相手の子がお酒弱そうだったりちょっと酔ってそうなら、「無理しないでね、お水か温かいお茶頼もうか？」ってすぐ声をかけてあげて。\n"
        "そういう優しさって、あとからじわじわ効いてくる🍵"
    ),
    (
        "相席ってその日のタイミングや相性もあるから、1組目で盛り上がらなくてもぜんぜん気にしなくて大丈夫。\n"
        "「まあこういう日もあるよね」って笑って切り替えられる人の方が、次の席でいい出会いがあったりするよ💪"
    ),

    # --- 8. 美容サロン激戦区×見せ場の法則 & クラブ回遊 ---
    (
        "土日の18時半〜20時頃に茶屋町・渋谷・新宿・心斎橋のラウンジに可愛い子が多いのは、周辺の美容院やサロン帰りのゴールデンタイムだから💇‍♀️\n"
        "「可愛くした日に直帰しない」1軒目組が多く、早い時間は狙い目だよ✨"
    ),
    (
        "相席した子の髪がツヤツヤだったりネイルが綺麗なら、乾杯してすぐ「今日美容院帰り？」「ネイル変えた？」って聞いてみて。\n"
        "時間かけて可愛くした所に最初の3分で気づける人は、それだけで好感度が段違いだよ💅"
    ),
    (
        "美容意識が高い子が多い街（渋谷・恵比寿・茶屋町・新宿・栄）のラウンジほど、男性の「肌・眉毛・青ヒゲ・サイズ感」もよく見られてる。\n"
        "距離70cmの空間だから、入店前に眉毛と肌テカリを整えるだけで勝率が変わるよ🪞"
    ),
    (
        "梅田・心斎橋・渋谷・新宿のように近くに大型クラブがある街のラウンジは、終電後の24時半〜25時半に女性が増える「深夜の第2波」が来やすい🎧\n"
        "クラブ前後の子が回遊するから、2軒目・深夜帯も強いんだよね🔥"
    ),
    (
        "「女性25人・男性2人」の極端な女余りの店って、実は30分以内に女性が移動しちゃう確率が55%以上ある。\n"
        "今は待たずに別店舗へ回る子が多いから、むしろ「男性がいて女性が増え始めてる店」の方が当たりやすいよ📊"
    ),
    (
        "都心の大型店は終電前がピークだけど、熊本・沖縄・金沢・仙台など地方の相席ラウンジは「深夜0時〜2時」に女性が121%へ逆転増加する🌙\n"
        "居酒屋終わりに一極集中するから、出張先なら22時半以降が狙い目だよ✈️"
    ),
    (
        "相席で一番コスパが悪いのは「微妙な空気のまま惰性で90分以上粘っちゃうこと」💸\n"
        "10分課金が嵩む前に「40分」で見切って、盛り上がるなら2軒目のバーへ、微妙なら近隣の穴場店へ回る方が3,000円以上浮くよ🍸"
    ),
    (
        "25日の給料日直後は男性が増えて競争率が上がるけど、実は「15〜24日の給料日前」はライバルが減って女性比率が平均+7.4%も高くなる📊\n"
        "雨の日の地下直結店や3連休の中日（日曜夜）もかなり狙い目の隠れ穴場日だよ☔️"
    ),
    (
        "1軒目が23時すぎに終わったあと、23時20分〜23時55分の「魔の40分間」に新規入店すると終電組の退店ラッシュに巻き込まれやすい🚃\n"
        "カフェで30分だけ待って、0時30分の「第2波」が戻ってからOLGやJISに入る方が圧倒的に勝率が高いよ🌙"
    ),
]


_FALLBACK_COLUMNS = [
    ("全51店舗「料金・イベント・現在人数」比較ボード活用ガイド", "/guide/price-event-comparison"),
    ("最初の10分で差がつく会話＆清潔感ガイド", "/guide/first-10-minutes"),
    ("相席ラウンジ前の身だしなみ・清潔感チェック", "/grooming"),
    ("1人（ソロ）vs 2人ペアの立ち回り比較", "/guide/solo-vs-pair"),
    ("2軒目・カフェやバーへの自然な誘い方", "/guide/second-venue"),
    ("オリエンタルラウンジ・ag・JISの違い徹底比較", "/guide/brand-comparison"),
    ("水曜・木曜の20時〜22時が一番の穴場な理由", "/guide/weekday-strategy"),
    ("金曜・土曜ピークの待ち時間回避＆ハシゴ攻略", "/guide/weekend-strategy"),
    ("新宿エリア3店舗の使い分け・穴場ガイド", "/guide/shinjuku"),
    ("なぜ美容サロン激戦区の相席ラウンジは美人が多いのか？", "/guide/beauty-area-stage"),
    ("ダンスクラブ街×深夜2回転目の回遊法則", "/guide/club-area-midnight"),
    ("「男性先行」の店ほど女性が増える？女性は待たない新常識", "/guide/male-lead-mechanics"),
    ("相席ラウンジ完全攻略ガイド（基本の立ち回り・お店選び）", "/guide/strategy"),
    ("全国53店舗の曜日・時間帯別 統計データ白書", "/stats"),
    ("相席ラウンジ実践コラム・攻略記事一覧", "/columns"),
]


def _column_for_tip_index(idx: int) -> tuple:
    """Return the primary (col_title, col_path) best matching TIPS_LIST[idx]."""
    if idx <= 9:
        return ("最初の10分で差がつく会話＆清潔感ガイド", "/guide/first-10-minutes")
    if idx <= 19:
        return ("相席ラウンジ前の身だしなみ・清潔感チェック", "/grooming")
    if idx <= 27:
        return ("1人（ソロ）vs 2人ペアの立ち回り比較", "/guide/solo-vs-pair")
    if idx <= 35:
        return ("2軒目・カフェやバーへの自然な誘い方", "/guide/second-venue")
    if idx == 37:
        return ("全51店舗「料金・イベント・現在人数」比較ボード活用ガイド", "/guide/price-event-comparison")
    if idx <= 39:
        return ("オリエンタルラウンジ・ag・JISの違い徹底比較", "/guide/brand-comparison")
    if idx == 40:
        return ("新宿エリア3店舗の使い分け・穴場ガイド", "/guide/shinjuku")
    if idx == 41:
        return ("水曜・木曜の20時〜22時が一番の穴場な理由", "/guide/weekday-strategy")
    if idx == 42:
        return ("金曜・土曜ピークの待ち時間回避＆ハシゴ攻略", "/guide/weekend-strategy")
    if idx == 43:
        return ("水曜・木曜の20時〜22時が一番の穴場な理由", "/guide/weekday-strategy")
    if idx == 44:
        import scheduled_articles
        if scheduled_articles.is_published("rain-payday"):
            return ("雨の日・給料日前・連休中日｜ライバルが減る隠れ穴場日データ", "/guide/rain-payday")
        return ("金曜・土曜ピークの待ち時間回避＆ハシゴ攻略", "/guide/weekend-strategy")
    if idx == 45:
        return ("ダンスクラブ街×深夜2回転目の回遊法則", "/guide/club-area-midnight")
    if idx == 46:
        return ("全国53店舗の曜日・時間帯別 統計データ白書", "/stats")
    if idx == 47:
        return ("全51店舗「料金・イベント・現在人数」比較ボード活用ガイド", "/guide/price-event-comparison")
    if idx == 48:
        import scheduled_articles
        if scheduled_articles.is_published("local-vs-urban"):
            return ("地方店 vs 都心大型店｜深夜0時に女性が121%へ逆転増加する理由", "/guide/local-vs-urban")
        return ("全国エリア別 リアルタイム混雑＆店舗ガイド", "/areas")
    if idx == 49:
        return ("新宿エリア3店舗の使い分け・穴場ガイド", "/guide/shinjuku")
    if idx in (50, 51, 52):
        return ("相席ラウンジ完全攻略ガイド（基本の立ち回り・お店選び）", "/guide/strategy")
    if idx in (53, 54, 55):
        return ("なぜ美容サロン激戦区の相席ラウンジは美人が多いのか？", "/guide/beauty-area-stage")
    if idx == 56:
        return ("ダンスクラブ街×深夜2回転目の回遊法則", "/guide/club-area-midnight")
    if idx == 57:
        return ("「男性先行」の店ほど女性が増える？女性は待たない新常識", "/guide/male-lead-mechanics")
    if idx == 58:
        import scheduled_articles
        if scheduled_articles.is_published("local-vs-urban"):
            return ("地方店 vs 都心大型店｜深夜0時に女性が121%へ逆転増加する理由", "/guide/local-vs-urban")
        return ("相席ラウンジ実践コラム・攻略記事一覧", "/columns")
    if idx == 59:
        import scheduled_articles
        if scheduled_articles.is_published("cost-break-even"):
            return ("90分粘る vs 40分で見切る｜滞在時間と料金の損益分岐点", "/guide/cost-break-even")
        return ("相席ラウンジ実践コラム・攻略記事一覧", "/columns")
    if idx == 60:
        import scheduled_articles
        if scheduled_articles.is_published("rain-payday"):
            return ("雨の日・給料日前・連休中日｜ライバルが減る隠れ穴場日データ", "/guide/rain-payday")
        return ("相席ラウンジ実践コラム・攻略記事一覧", "/columns")
    if idx == 61:
        return ("「23時20分、店内の空気が変わる」——魔の40分間と街別ハシゴ法則", "/guide/last-train-cliff")
    return ("相席ラウンジ実践コラム・攻略記事一覧", "/columns")


def _is_column_used_today(col_path: str, date_str: str) -> bool:
    try:
        import job_state
        return job_state.is_done(f"x_col_used:{date_str}:{col_path}")
    except Exception:
        return False


def _select_tip_index(slot_label: str = "12:00", tip_index: int = None, now_jst: datetime.datetime = None) -> int:
    now_jst = now_jst or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    n = len(TIPS_LIST)
    if tip_index is not None and 0 <= tip_index < n:
        return tip_index

    date_str = now_jst.strftime("%Y-%m-%d")
    day_of_year = now_jst.timetuple().tm_yday

    # Select 12:00 index (avoiding any column already posted earlier today, e.g. scheduled release)
    base_12 = (day_of_year * 2) % n
    idx_12 = base_12
    for step in range(n):
        cand = (base_12 + step) % n
        _, cpath = _column_for_tip_index(cand)
        if not _is_column_used_today(cpath, date_str):
            idx_12 = cand
            break

    if slot_label == "12:00":
        return idx_12

    # For 18:00 (or any non-12:00 slot): start from a well-separated offset (+23)
    # and guarantee we NEVER pick the same tip or same column path as 12:00 or any column used today.
    _, col_12_path = _column_for_tip_index(idx_12)
    base_18 = (day_of_year * 2 + 23) % n
    for step in range(n):
        cand = (base_18 + step) % n
        if cand == idx_12:
            continue
        _, cpath = _column_for_tip_index(cand)
        if cpath == col_12_path:
            continue
        if _is_column_used_today(cpath, date_str):
            continue
        return cand

    return (idx_12 + 23) % n


def get_tip_for_slot(slot_label: str = "12:00", tip_index: int = None, now_jst: datetime.datetime = None) -> str:
    """
    Select a tip based on the day of the year and slot.
    1st post is kept completely URL-free for clean appearance and maximum X organic reach.
    The site URL and matching column URL are attached automatically in the 2nd post (reply).
    """
    idx = _select_tip_index(slot_label=slot_label, tip_index=tip_index, now_jst=now_jst)
    raw_tip = TIPS_LIST[idx].strip()
    footer_tags_only = "\n\n📖 詳しいコラム・現在の男女比はリプ欄へ👇\n#相席ラウンジ"

    candidate = f"{raw_tip}{footer_tags_only}"
    if x_poster.compute_twitter_weight(candidate) <= 276:
        return candidate

    short_tags = "\n\n👇詳細・現在人数はリプ欄へ\n#相席ラウンジ"
    if x_poster.compute_twitter_weight(f"{raw_tip}{short_tags}") <= 276:
        return f"{raw_tip}{short_tags}"

    minimal_tags = "\n#相席ラウンジ"
    if x_poster.compute_twitter_weight(f"{raw_tip}{minimal_tags}") <= 276:
        return f"{raw_tip}{minimal_tags}"

    return raw_tip


def resolve_tip_column(slot_label: str = "12:00", tip_index: int = None, now_jst: datetime.datetime = None) -> tuple:
    """Return (col_title, col_path) guaranteed not to duplicate another column posted on the same day."""
    now_jst = now_jst or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    date_str = now_jst.strftime("%Y-%m-%d")
    idx = _select_tip_index(slot_label=slot_label, tip_index=tip_index, now_jst=now_jst)
    col_title, col_path = _column_for_tip_index(idx)

    forbidden = set()
    if slot_label != "12:00" and tip_index is None:
        idx_12 = _select_tip_index(slot_label="12:00", now_jst=now_jst)
        forbidden.add(_column_for_tip_index(idx_12)[1])

    if col_path in forbidden or _is_column_used_today(col_path, date_str):
        for fb_title, fb_path in _FALLBACK_COLUMNS:
            if fb_path not in forbidden and not _is_column_used_today(fb_path, date_str):
                col_title, col_path = fb_title, fb_path
                break

    return col_title, col_path


def get_tip_reply_text(slot_label: str = "12:00", tip_index: int = None, now_jst: datetime.datetime = None) -> str:
    """Build the 2nd post (self-reply) with the matching column URL + real-time ranking URL."""
    site_url = os.environ.get("PUBLIC_SITE_URL", "https://aiseki-navi.com").rstrip("/")
    col_title, col_path = resolve_tip_column(slot_label=slot_label, tip_index=tip_index, now_jst=now_jst)

    return (
        f"📖 詳しい解説コラム：{col_title}👇\n"
        f"{site_url}{col_path}"
    )


def run_scheduled_tip_post(slot_label: str = "12:00", force: bool = False, tip_index: int = None) -> dict:
    """
    Execute daily tip posting to X (Twitter).
    slot_label: '12:00' or '18:00'
    Posts a clean URL-free main post, followed by an automatic reply with the column + ranking URLs.
    """
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    date_str = now_jst.strftime("%Y-%m-%d")
    dedup_key = f"x_tip:{date_str}_{slot_label}"

    # 投稿済みフラグは job_state（Google Sheets 永続化）で管理し、再起動・再デプロイ後の二重投稿を防ぐ
    import job_state
    if not force and job_state.is_done(dedup_key):
        logger.info(f"X tip post already sent for {dedup_key}, skipping.")
        return {"status": "skipped", "reason": "Already posted for this slot today"}

    idx = _select_tip_index(slot_label=slot_label, tip_index=tip_index, now_jst=now_jst)
    text = get_tip_for_slot(slot_label=slot_label, tip_index=idx, now_jst=now_jst)
    if not text:
        return {"status": "error", "detail": "Empty tip text"}

    if force:
        text = f"{text}\n(⏱️{now_jst.strftime('%H:%M')})"

    col_title, col_path = resolve_tip_column(slot_label=slot_label, tip_index=idx, now_jst=now_jst)
    site_url = os.environ.get("PUBLIC_SITE_URL", "https://aiseki-navi.com").rstrip("/")
    reply_text = f"📖 詳しい解説コラム：{col_title}👇\n{site_url}{col_path}"

    result = x_poster.post_to_x(text, reply_text=reply_text)
    if result.get("status") == "success" and not force:
        job_state.mark_done(dedup_key)
        job_state.mark_done(f"x_col_used:{date_str}:{col_path}")

    result["tweet_text"] = text
    result["reply_text"] = reply_text
    result["column_path"] = col_path
    result["slot"] = slot_label
    return result
