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
]


def _select_tip_index(slot_label: str = "12:00", tip_index: int = None) -> int:
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    if tip_index is not None and 0 <= tip_index < len(TIPS_LIST):
        return tip_index
    slot_offset = 0 if slot_label == "12:00" else 1
    day_of_year = now_jst.timetuple().tm_yday
    return (day_of_year * 2 + slot_offset) % len(TIPS_LIST)


def get_tip_for_slot(slot_label: str = "12:00", tip_index: int = None) -> str:
    """
    Select a tip based on the day of the year and slot.
    1st post is kept completely URL-free for clean appearance and maximum X organic reach.
    The site URL and matching column URL are attached automatically in the 2nd post (reply).
    """
    idx = _select_tip_index(slot_label=slot_label, tip_index=tip_index)
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


def get_tip_reply_text(slot_label: str = "12:00", tip_index: int = None) -> str:
    """Build the 2nd post (self-reply) with the matching column URL + real-time ranking URL."""
    site_url = os.environ.get("PUBLIC_SITE_URL", "https://aiseki-navi.com").rstrip("/")
    idx = _select_tip_index(slot_label=slot_label, tip_index=tip_index)

    if idx <= 9:
        col_title = "最初の10分で差がつく会話＆清潔感ガイド"
        col_path = "/guide/first-10-minutes"
    elif idx <= 19:
        col_title = "相席ラウンジ前の身だしなみ・清潔感チェック"
        col_path = "/grooming"
    elif idx <= 27:
        col_title = "1人（ソロ）vs 2人ペアの立ち回り比較"
        col_path = "/guide/solo-vs-pair"
    elif idx <= 35:
        col_title = "2軒目・カフェやバーへの自然な誘い方"
        col_path = "/guide/second-venue"
    elif idx <= 40:
        col_title = "オリエンタルラウンジ・ag・JISの違い徹底比較"
        col_path = "/guide/brand-comparison"
    elif idx <= 49:
        col_title = "水曜・木曜の20時〜22時が一番の穴場な理由"
        col_path = "/guide/weekday-strategy"
    else:
        col_title = "相席ラウンジ実践コラム・攻略記事一覧"
        col_path = "/columns"

    return (
        f"📖 {col_title}\n"
        f"{site_url}{col_path}\n\n"
        f"🔍 全国53店舗のリアルタイム男女比・人数ランキング\n"
        f"{site_url}/"
    )


def run_scheduled_tip_post(slot_label: str = "12:00", force: bool = False, tip_index: int = None) -> dict:
    """
    Execute daily tip posting to X (Twitter).
    slot_label: '12:00' or '18:00'
    Posts a clean URL-free main post, followed by an automatic reply with the column + ranking URLs.
    """
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    dedup_key = f"x_tip:{now_jst.strftime('%Y-%m-%d')}_{slot_label}"

    # 投稿済みフラグは job_state（Google Sheets 永続化）で管理し、再起動・再デプロイ後の二重投稿を防ぐ
    import job_state
    if not force and job_state.is_done(dedup_key):
        logger.info(f"X tip post already sent for {dedup_key}, skipping.")
        return {"status": "skipped", "reason": "Already posted for this slot today"}

    idx = _select_tip_index(slot_label=slot_label, tip_index=tip_index)
    text = get_tip_for_slot(slot_label=slot_label, tip_index=idx)
    if not text:
        return {"status": "error", "detail": "Empty tip text"}

    if force:
        text = f"{text}\n(⏱️{now_jst.strftime('%H:%M')})"

    reply_text = get_tip_reply_text(slot_label=slot_label, tip_index=idx)
    result = x_poster.post_to_x(text, reply_text=reply_text)
    if result.get("status") == "success" and not force:
        job_state.mark_done(dedup_key)

    result["tweet_text"] = text
    result["reply_text"] = reply_text
    result["slot"] = slot_label
    return result
