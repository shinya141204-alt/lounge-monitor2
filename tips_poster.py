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

# 62 sharp, human-toned tips blending spicy field truths (辛口・図星) and genuine female perspectives (女性目線のリアル).
# Each tip is sized so that the tip + footer + hashtag stays strictly within X API's 280-weight limit.
TIPS_LIST = [
    # --- 1. 会話・空気感・乾杯10分のリアル (idx 0..9 -> /guide/first-10-minutes) ---
    (
        "相席して最初の3分で「休みの日なにしてるの？」「どこ住み？」って履歴書確認を始める人、実はその瞬間に女子側のシャッターが半分閉まってる。\n"
        "最初は「1軒目どのへんで飲んでたの？」くらいの雑談から入る方が圧倒的に空気がほぐれるよ🍻"
    ),
    (
        "正直、相席で一番空気が凍るのは「聞かれてもいない仕事の武勇伝」と「忙しさアピール」。\n"
        "女の子も仕事終わりに息抜きで来てるから、すごい人に見せようとするより「それわかる笑」って笑える隙がある人の方が結局モテるんだよね。"
    ),
    (
        "女子目線のリアルな話、テーブルの上にスマホを「画面上向き」で置いてチラチラ通知を見始めたら、かなり危険信号📱\n"
        "本当に居心地いい席だとスマホはバッグの中か裏返しになる。質問攻めをやめて食べ物の話に切り替えよ🍽️"
    ),
    (
        "「何歳に見える？」って聞かれて、ただマイナス3歳くらいを答えるだけだと『はいはい営業トークね』って思われがち。\n"
        "「落ち着いてるから同い年くらいかと思ったけど、笑うと年下っぽいね」くらい温度差をつけると一気に打ち解けるよ🍸"
    ),
    (
        "「女の子はタダで飲めていいよね〜」って冗談でも言っちゃう男性、その一言で恋愛対象から外れてるので要注意💸\n"
        "女子側は1時間半かけてメイクして髪巻いてヒールで来てる。『時間と準備のコスト』をわかってくれる人が一番強い💄"
    ),
    (
        "会話が盛り上がらない席の9割は、無意識に「一問一答の面接官」になってる。\n"
        "相手が答えたらすぐ次の質問をぶつけるんじゃなくて、「いいなそれ、実は俺もさ〜」って自分のちょっと抜けた話を1つ混ぜるだけで急に会話が転がるよ。"
    ),
    (
        "「趣味なに？」って聞かれると女子側も身構えるけど、「最近なんか沼ってるものある？ネトフリでも食べ物でも！」って聞かれると急に喋りやすくなる。\n"
        "そこから「じゃあ今度そこ食べに行こ」に繋げるのが一番スマート☕️"
    ),
    (
        "乾杯して最初の5分で、ネイルやピアスの色、巻いた髪のツヤにさらっと気づいて褒められる人はやっぱり別格💅\n"
        "顔をベタ褒めされると警戒されるけど、「準備に時間をかけたディテール」を褒められて嫌な気分の女の子はいないよ。"
    ),
    (
        "もし乾杯して10分で沈黙が落ちたら、無理にウケを狙うより「ここのフード何が当たりか店員さんに聞いてみよっか」って巻き込むのが正解。\n"
        "メニュー選びを共同作業にすると、初対面の硬い空気が一回リセットされるよ🍕"
    ),
    (
        "自己紹介で肩書きをキッチリ語る人ほど「いい人だけど堅そう」で終わっちゃう。\n"
        "「〇〇関係の仕事してて、最近は仕事終わりにサウナか町中華ばっかり開拓してる笑」くらい生活感を出した方が、相手もツッコミやすいよ🥟"
    ),

    # --- 2. 距離70cmの身だしなみ・女性目線のシビア査定 (idx 10..19 -> /grooming) ---
    (
        "ラウンジの照明が暗いからって油断してる人が多いけど、ソファ席の距離70cmだと「夕方の肌テカリ」と「唇のカサつき」は残酷なくらい見えてる🪞\n"
        "エレベーター乗る前にコンビニの汗拭きシートとリップ1本使うだけで勝率が変わるよ。"
    ),
    (
        "女子トイレの反省会で意外とよく出る減点ポイントが「ジャケットは綺麗なのに靴がボロボロ」なパターン👟\n"
        "ソファ席に案内されるときや席を立つとき、足元って一番目に入るから、出る前に靴の汚れだけは見ておいた方がいいよ。"
    ),
    (
        "狭いボックス席でハイブランドの強い香水をプンプンさせて来ると、食事の邪魔になって1杯で席替えされやすい🫧\n"
        "70cmの距離で一番ウケがいいのは、香水じゃなくて柔軟剤かヘアバームがふわっと香るくらいの清潔感。"
    ),
    (
        "相席に気合い入れすぎて全身ハイブランドのロゴだらけで行くと、女子側からは「遊んでそう・圧が強い」って警戒されがち👔\n"
        "結局、シワのない無地ニットやシャツをジャストサイズで着てる『余裕のある普通』が一番信頼される。"
    ),
    (
        "間接照明の店内で前髪が重すぎると、目元の影が落ちて実物より3割くらい暗い人に見えちゃう✂️\n"
        "眉毛のボサボサだけ整えておでこを少し出すだけで、乾杯の瞬間の「あ、爽やかな人だ」っていう第一印象は作れるよ。"
    ),
    (
        "1軒目の居酒屋でニンニク系やタバコを挟んでそのまま相席に来る人、本人は気づいてないけど距離70cmだと一発でバレてる🦷\n"
        "どんなに話が面白くても口臭ひとつで全部台無しになるから、入店前のブレスケアだけは絶対ケチっちゃダメ。"
    ),
    (
        "タッチパネルで注文するときやグラスを持つとき、女の子の視線が必ず行くのが「指先と袖口」✋\n"
        "ここで清潔感がないと生理的に恋愛対象から外されちゃう。顔立ちより『手入れされてる感』の方がよっぽど大事。"
    ),
    (
        "インスタやLINEを交換するとき、差し出したスマホの画面がバキバキに割れてたり指紋でベタベタだと、その瞬間にちょっと冷められる📱\n"
        "意外と見られてる「スマホの清潔感」、入店前にサッと拭いておくだけでスマートだよ。"
    ),
    (
        "高い服を着てるかどうかなんて女子はほとんど見てないけど、「ニットの毛玉」と「首元のヨレ」は3秒で見抜かれてる👕\n"
        "ユニクロでも全然いいから、くたびれてない服を着ていくのが相手への最低限の礼儀だよね。"
    ),
    (
        "「清潔感って結局イケメンってことでしょ？」って誤解されがちだけど、女子が見てるのは『隣で歩いて恥ずかしくないサイズ感と肌ツヤ』だけ✨\n"
        "ダボダボすぎない服と整った眉・髪だけで、上位2割の印象には普通に入れるよ。"
    ),

    # --- 3. 2人組ペアの立ち回り・女子トイレ会議の裏側 (idx 20..27 -> /guide/solo-vs-pair) ---
    (
        "男2人で対面ソファに座るとき、タイプの子と真正面に座っちゃうと圧が強すぎて一問一答になりがち👥\n"
        "あえて「斜め向かい」の位置に座る方が、目線も自然に外せて2人だけの会話も作りやすいよ。"
    ),
    (
        "相席の勝率って、実は「誰と組んで行くか」で7割決まる🤝\n"
        "2人とも俺が俺がで喋るペアや、逆に2人とも待ち姿勢のペアは苦戦しやすい。片方がパスを出して片方が広げられるコンビはやっぱり無双する。"
    ),
    (
        "場を盛り上げようとして、一緒に来た友達をイジって下げる男ペア、女子目線だと『性格悪そう…』で2人まとめて減点されてるよ⚠️\n"
        "「こいつ仕事めっちゃ頼りになるんだよ」って相方を自然に立てられるペアの方が100倍モテる。"
    ),
    (
        "女子2人が乾杯して15分で「ちょっとお手洗い行こっか」って同時に立ったら、それは鏡の前で『この席どうする？』の作戦会議タイム💄\n"
        "戻ってきたあとに食事のペースが急に早くなったら、切り上げ準備のサインだよ。"
    ),
    (
        "4人で飲んでるとき、自分のタイプじゃない方の女の子を空気みたいに扱う人は絶対うまくいかない🚫\n"
        "女子ペアは運命共同体だから、友達側が退屈した時点で「明日早いし帰ろっか」ってブロックが入る。4人全員を楽しませる人が最後に勝つよ。"
    ),
    (
        "もし相方が気になる子と良い空気になってたら、もう1人の女の子を全力で楽しませるアシスト役に回れるかが男の器🔑\n"
        "友達側の満足度が高いと、「もう少し4人で2軒目行こ！」って背中を押してもらえるよ。"
    ),
    (
        "入店前に相方と「微妙だったら40分で店変えよう」「当たったら裏のあのバー誘おう」って2行だけ握っておくのが鉄則👔\n"
        "お会計前に目配せでモタつかないペアは、女子から見てもこなれてて余裕があるように映るよ。"
    ),
    (
        "男2人だけで盛り上がって、会社の愚痴や大学時代の内輪ノリを始めた瞬間、女子2人は心の中で『タダ飯消化モード』に切り替わる🍻\n"
        "常に「初対面の4人全員が乗れる話題か？」を意識できるだけで、席の寿命がぜんぜん違うよ。"
    ),

    # --- 4. 連絡先交換・グループLINE墓場・2軒目誘導 (idx 28..35 -> /guide/second-venue) ---
    (
        "お会計の直前に「4人でグループLINE作ろう！」って言うの、誰も傷つかないけど9割が翌日に通知オフされる『社交辞令の墓場』になりがち🪦\n"
        "会話が一番盛り上がった瞬間に「さっきのお店、インスタDMで送るね！」って個別で繋がる方が圧倒的に次に繋がるよ。"
    ),
    (
        "2軒目に誘うとき「もう1軒飲みに行こうよ」だと『長くなりそう・重そう』で断られやすい。\n"
        "「すぐ裏に夜パフェ（美味しいコーヒー）出せるバーあるから30分だけ寄らない？」って『甘いもの×短時間』で切ると驚くほど通りやすい🍨"
    ),
    (
        "盛り上がった席で「居心地いいから」って店内で2時間粘るのが一番の負けパターン⏰\n"
        "23時を過ぎると女子側は一気に終電とメイク崩れが気になり出すから、余韻が残る45〜50分目でサクッと外の空気へ連れ出す方がスマート。"
    ),
    (
        "解散したあとの1通目、家に着いてから長文を送るより「改札で別れて5分以内」に1行だけ送る方が返信率が段違い💌\n"
        "帰りの電車でスマホを見てる瞬間に「今日めっちゃ笑った！気をつけて帰ってね」って届くのが一番効く。"
    ),
    (
        "2軒目を断られた瞬間にあからさまにテンション下げる人、せっかく交換した連絡先までその場で死んでるので要注意😇\n"
        "「明日早いもんね、今日乾杯できて楽しかった！」って秒で爽やかに解散できる人だけが、翌週のサシ飯に勝てる。"
    ),
    (
        "女子が交換直後に絶対チェックしてるのが「LINEのアイコンと名前」📷\n"
        "キメキメの自撮りや加工強めの写真より、友達に撮ってもらった自然な笑顔か旅先・美味しいご飯の写真にしておくだけで安心感がぜんぜん違うよ。"
    ),
    (
        "せっかく「2軒目行こっか」ってなったのに、外に出てから路上で「どこ空いてるかな…」って食べログ検索し始めると魔法が解ける🚶\n"
        "よく行く街なら、ラウンジから徒歩3分以内の静かなバーか夜カフェを1軒だけポケットに入れておこう。"
    ),
    (
        "ガチガチにLINEを聞き出すより「インスタ交換しよ！」の方が今っぽくて警戒されにくい📸\n"
        "普段の趣味やストーリーの空気感でお互い安心できるし、数日後にグルメ投稿へ軽くリアクションする方が自然にデートへ誘えるよ。"
    ),

    # --- 5. お店のシステム・ブランド別の本音使い分け (idx 36..39) ---
    (
        "噛み合わない席で「悪い気もするし…」っていい人ぶって60分・90分と延長しても、実は女子側も『いつ切り上げようかな』って時計を見てることが多い💡\n"
        "お互いの貴重な夜を守るためにも、合わない席は35〜40分で笑顔でリセットするのが一番優しい。"
    ),
    (
        "相席ラウンジって実は「曜日・時間帯・イベント有無」で体感コスパが別物になる📱\n"
        "公式アプリのチャージ優待はもちろん、今どの店に人が集まってるかを見てから入るだけで、無駄な待ち課金をゼロにできるよ。"
    ),
    (
        "オリエンタルラウンジ・ag・JIS、看板の雰囲気を知らずに入ると普通にミスマッチする🍸\n"
        "落ち着いて大人っぽく飲むならオリエンタルラウンジ、20代前半のノリでワイワイいくならag、高回転でサクッと相席するならJISって使い分けるのが鉄則。"
    ),
    (
        "女の子が乾杯より前に100%チェックしてるのが「店員さんへの態度」🤵‍♂️\n"
        "案内やドリンク提供のときにタメ口だったり横柄な人は、どれだけ見た目が良くてもその瞬間に『なし判定』されてるのでほんと気をつけて。"
    ),

    # --- 6. 曜日・時間帯・街別の残酷なギャップ (idx 40..49) ---
    (
        "新宿や渋谷みたいに徒歩5分圏内に何店舗もある街で、1店舗目の「男性10組待ち」に意地になって並ぶのが一番もったいない👟\n"
        "目と鼻の先の別ブランドを見たら女性過多で即座れた、なんて毎晩のように起きてるからね。"
    ),
    (
        "金曜22時のピークに突撃して受付待ちで消耗するくらいなら、実は「水曜・木曜の20時半」に行った方がライバルが少なくて落ち着いて話せる子に当たりやすい🍷\n"
        "勝てる人ほど、あえて週末ど真ん中を外して戦ってる。"
    ),
    (
        "金土にどうしても行くなら、全員が1軒目を終えて押し寄せる21時半より前の「19:45〜20:30」に先回りして座っておくのが最強🏃‍♂️\n"
        "後から入ってくる1軒目帰りの女子組を、余裕のある状態で迎えられるよ。"
    ),
    (
        "意外と穴場なのが「日曜の20時〜22時」🌙\n"
        "月曜休みの美容師さん・アパレル・医療系の女の子がふらっと飲みに来てることが多くて、金土みたいなガヤガヤ感がないぶん会話の密度がすごく高い。"
    ),
    (
        "女子の本音で言うと「雨の日に傘さして路面店まで歩くと前髪と靴が死ぬ」から、雨夜は地下街・駅直結のラウンジ一択になる☔️\n"
        "しかも雨だと男性の出足が鈍るから、駅直結店だけ女性比率が跳ね上がるボーナス日になりやすいよ。"
    ),
    (
        "終電後の0時半を回ると、「とりあえずタダ飯だけ」の層が帰って「まだ飲み足りない・誰かと話したい」テンションの子だけが残る🔥\n"
        "男性の待ち列も消える時間帯だから、深夜に強い店舗を知っておくと2軒目・3軒目でかなり使える。"
    ),
    (
        "エレベーターが開いた瞬間にスーツ姿の男だらけで肩を落とすあの絶望感、向かう前の10秒チェックで100%防げる📱\n"
        "気合いとノリだけで突撃せず、リアルタイムの男女人数を見て「今勝てる店」だけ踏むのが一番スマート。"
    ),
    (
        "女子側のリアルな心理として、新作スイーツや美容ギフト系のイベント日はやっぱり友達を誘う口実になりやすい🎯\n"
        "店舗選びで迷ったら、その夜に女性向けキャンペーンが走ってる店からチェックすると早い時間から当たりやすいよ。"
    ),
    (
        "出張で札幌・仙台・名古屋・福岡・熊本あたりに行った夜、ホテルで1人飲みするより現地のラウンジを覗くと想像以上に盛り上がる✈️\n"
        "「今日出張で来てて、この街の美味しい店教えてほしくて！」は最強の自然な会話フックになるよ。"
    ),
    (
        "同じ新宿でも「あっちの店は男余りでピリついてるのに、徒歩4分のこっちは女性比率65%で穏やか」っていう格差がリアルタイムで起きてる📊\n"
        "店の知名度じゃなくて『今この瞬間の比率』を見る癖をつけると世界が変わるよ。"
    ),

    # --- 7. メンタル・余裕・男女の非対称性 (idx 50..52 -> /guide/strategy) ---
    (
        "「10分ごとに課金されてるから早く結果出さなきゃ！」って焦ってるオーラは、悲しいくらい相手に伝わる🍷\n"
        "ガツガツ口説こうとする人より、「今日ちょっと楽しく飲めたらラッキー」くらいの余白がある人の方が結局2軒目に誘っても頷いてもらえる。"
    ),
    (
        "お酒が弱そうな子や2軒目で少し眠そうな子に「もっと飲もうよ！」って煽るのは最悪手🍵\n"
        "「お水か温かいお茶頼もうか？」って先回りしてペースを守ってあげられる人の方が、女子からの信頼度が段違いに高いよ。"
    ),
    (
        "女性2人組でラウンジ行くときのリアルなコツ：『女の子がいっぱい入ってる人気店』に行くと、女余りで30分放置されて気まずくなりやすい💄\n"
        "あえて『男性が先行して入ってる大型店』を選ぶ方が、待ち0分で歓迎されて圧倒的にラクだよ！"
    ),

    # --- 8. 美容サロン激戦区・深夜第2波・データ裏法則 (idx 53..61) ---
    (
        "土日の18時半〜20時頃に茶屋町・渋谷・新宿・心斎橋のラウンジのレベルが高いのは、周辺の美容サロン帰りのゴールデンタイムだから💇‍♀️\n"
        "「せっかく髪もネイルも整えた日に直帰したくない」1軒目女子が入る早い時間はかなりの狙い目✨"
    ),
    (
        "サロン帰りで髪がツヤツヤな子やネイルを変えたばかりの子に、乾杯3分で「今日美容院帰り？色めっちゃ綺麗だね」って気づける人は本当に強い💅\n"
        "外見そのものじゃなく『今日かけた手間』を褒めるのが大人の余裕。"
    ),
    (
        "美容意識が高い子が集まるエリア（渋谷・恵比寿・茶屋町・新宿・栄）ほど、女子側の「清潔感センサー」もシビア🪞\n"
        "70cmの距離で眉毛・肌テカリ・服のヨレだけは誤魔化せないから、入店前3分の身だしなみチェックが直結するよ。"
    ),
    (
        "梅田・心斎橋・渋谷・新宿のように大型クラブが近い街のラウンジは、終電後の0時半〜1時半にもう一度女性が増える「深夜の第2波」が来る🎧\n"
        "終電で終わったと思わず、深夜維持率の高い店を選ぶのが2軒目組の勝ち筋🔥"
    ),
    (
        "「女性25人・男性2人！」みたいな極端な女余りの店を見て歓喜して向かうと、着いた頃には半分以上いなくなってることがある📊\n"
        "今の女子組は待たされる店を30分で見切って別店舗へ回るから、『男性も適度にいて女性が増え始めてる店』が一番当たるよ。"
    ),
    (
        "東京の感覚で地方（熊本・沖縄・金沢・仙台など）のラウンジに20時に行くと静かだけど、実は「深夜0時〜2時」に女性が121%へ逆転急増する🌙\n"
        "地方は居酒屋終わりの2軒目が1店舗に一極集中するから、出張先なら22時半以降が本番だよ✈️"
    ),
    (
        "相席で一番お財布もメンタルも削られるのが「盛り上がりきらない席で気を使って90分粘り、改札で1万近いレシートを見て苦笑いする夜」💸\n"
        "40分を分岐点にして、弾むなら外のバーへ、噛み合わないなら好バランス店へ切り替える方が圧倒的に賢い🍸"
    ),
    (
        "25日の給料日直後は気合いを入れた男性が殺到して待ち列だらけになるけど、実は「15〜24日の給料日前」はライバルが減って女性比率が平均+7.4%も跳ね上がる📊\n"
        "みんなが動かない雨の地下直結店や給料日前こそ、一番イージーに勝てる穴場日☔️"
    ),
    (
        "1軒目が23時すぎに終わったあと、23時20分〜23時55分の「魔の40分間」に飛び込むと終電帰りの退店ラッシュに巻き込まれて空振りしやすい🚃\n"
        "夜カフェで30分だけ時間を潰して、0時30分の「第2波」が戻ってからオリエンタルラウンジやJISに入る方が勝率が段違いだよ🌙"
    ),
    (
        "大阪で梅田の東通りが男性40分待ちの夜、そのまま突っ立って待つより「徒歩6分の茶屋町」か「御堂筋線9分の難波」を見た方が圧倒的に早い🐙\n"
        "20時台はJIS茶屋町、22時台は裏天満やオリエンタルラウンジ難波って時間帯で主役が入れ替わるから1店決め打ちは損だよ📊"
    ),
    (
        "金曜22時に「女性比60%」を見て向かったのに、着いた頃には男余りになってた経験ない？📊\n"
        "22時台は1次会終わりの男が一斉に向かうから約3割が到着時に逆転する。22時以降は『女性比70%超』の店を選ぶのが鉄則⚡"
    ),
    (
        "相席で会話が死んだとき「席替え頼んだら失礼かな…」と迷う男性、実は女子側も内心『誰か助けて』って思ってる。\n"
        "「明日早いですよね、そろそろ交代お願いしましょうか」って男側から笑顔で切り出せる人は、むしろ神対応扱いされるよ🍸"
    ),
]


_FALLBACK_COLUMNS = [
    ("相席ラウンジの席替え完全攻略｜言えない男と察する女の心理学", "/guide/table-change-protocol"),
    ("金土22時の「女性比60%」が3割逆転する群衆殺到パラドックス", "/guide/crowd-rush-paradox"),
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
    if idx == 62:
        import scheduled_articles
        if scheduled_articles.is_published("osaka-kita-vs-minami"):
            return ("梅田キタ vs 難波ミナミ｜大阪全9店舗の南北ハシゴ法則", "/guide/osaka-kita-vs-minami")
        return ("大阪・梅田エリアのリアルタイム混雑＆攻略", "/area/umeda")
    if idx == 63:
        return ("金土22時の「女性比60%」が3割逆転する群衆殺到パラドックス", "/guide/crowd-rush-paradox")
    if idx == 64:
        import scheduled_articles
        if scheduled_articles.is_published("table-change-protocol"):
            return ("相席ラウンジの席替え完全攻略｜言えない男と察する女の心理学", "/guide/table-change-protocol")
        return ("相席ラウンジ実践コラム・攻略記事一覧", "/columns")
    return ("相席ラウンジ実践コラム・攻略記事一覧", "/columns")


def _is_column_used_today(col_path: str, date_str: str) -> bool:
    try:
        import job_state
        return job_state.is_done(f"x_col_used:{date_str}:{col_path}")
    except Exception:
        return False


def _get_pdca_priority_paths() -> list:
    try:
        import pdca_engine
        return list((pdca_engine.get_active_directives(allow_blocking=False) or {}).get("priority_column_paths") or [])
    except Exception:
        return []


def _is_column_public_now(col_path: str, now_jst: datetime.datetime = None) -> bool:
    if not col_path or not col_path.startswith("/"):
        return False
    if col_path.startswith("/guide/"):
        slug = col_path.split("/guide/", 1)[1].strip("/")
        try:
            import scheduled_articles
            sitem = scheduled_articles.get_article_by_slug(slug)
            if sitem is not None:
                return scheduled_articles.is_published(sitem, now_jst=now_jst)
        except Exception:
            pass
    return True


def _title_for_column_path(col_path: str) -> str:
    for t, p in _FALLBACK_COLUMNS:
        if p == col_path:
            return t
    try:
        import scheduled_articles
        for item in scheduled_articles.get_published_articles():
            if item.get("path") == col_path:
                return item.get("short_title") or item.get("title") or "相席ラウンジ実践コラム"
    except Exception:
        pass
    try:
        import column_editorial
        for c in column_editorial.EXISTING_STATIC_COLUMNS:
            if c.get("path") == col_path:
                return c.get("title") or "相席ラウンジ実践コラム"
    except Exception:
        pass
    return "相席ラウンジ実践コラム・攻略記事"


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

    # PDCA closed-loop boost: prioritize high-CTR or newly released columns in the evening slot
    prio_paths = [p for p in _get_pdca_priority_paths() if _is_column_public_now(p, now_jst=now_jst)]
    if prio_paths:
        for step in range(n):
            cand = (base_18 + step) % n
            if cand == idx_12:
                continue
            _, cpath = _column_for_tip_index(cand)
            if cpath in prio_paths and cpath != col_12_path and not _is_column_used_today(cpath, date_str):
                return cand

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


def is_standalone_url_free_tip(
    slot_label: str = "18:00",
    now_jst: datetime.datetime = None,
    tip_index: int = None,
) -> bool:
    """
    On alternating days (`toordinal() % 2 == 1`) for the automated 18:00 evening slot,
    send a pure 1-post conversational observation WITHOUT any self-reply URL so X's organic
    algorithm sees natural non-promotional engagement alongside thread days.
    """
    if tip_index is not None or slot_label != "18:00":
        return False
    now_jst = now_jst or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    return (now_jst.toordinal() % 2) == 1


def get_tip_for_slot(
    slot_label: str = "12:00",
    tip_index: int = None,
    now_jst: datetime.datetime = None,
    standalone_no_reply: bool = False,
) -> str:
    """
    Select a tip based on the day of the year and slot.
    1st post is kept completely URL-free for clean appearance and maximum X organic reach.
    When `standalone_no_reply=True`, omits the 'リプ欄へ👇' pointer for a pure 1-tweet reading experience.
    """
    idx = _select_tip_index(slot_label=slot_label, tip_index=tip_index, now_jst=now_jst)
    raw_tip = TIPS_LIST[idx].strip()

    if standalone_no_reply:
        clean_tags = "\n\n#相席ラウンジ"
        if x_poster.compute_twitter_weight(f"{raw_tip}{clean_tags}") <= 276:
            return f"{raw_tip}{clean_tags}"
        minimal_tags = "\n#相席ラウンジ"
        if x_poster.compute_twitter_weight(f"{raw_tip}{minimal_tags}") <= 276:
            return f"{raw_tip}{minimal_tags}"
        return raw_tip

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


def resolve_tip_column(
    slot_label: str = "12:00",
    tip_index: int = None,
    now_jst: datetime.datetime = None,
    prefer_pdca: bool = None,
) -> tuple:
    """Return (col_title, col_path) guaranteed not to duplicate another column posted on the same day."""
    now_jst = now_jst or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    date_str = now_jst.strftime("%Y-%m-%d")
    if prefer_pdca is None:
        prefer_pdca = (tip_index is None and slot_label != "12:00")

    idx = _select_tip_index(slot_label=slot_label, tip_index=tip_index, now_jst=now_jst)
    col_title, col_path = _column_for_tip_index(idx)

    forbidden = set()
    if slot_label != "12:00" and (tip_index is None or prefer_pdca):
        idx_12 = _select_tip_index(slot_label="12:00", now_jst=now_jst)
        forbidden.add(_column_for_tip_index(idx_12)[1])

    # If PDCA has a newly published dynamic editorial column or top-CTR column not yet promoted today, prioritize it
    if prefer_pdca:
        prio_paths = _get_pdca_priority_paths()
        if prio_paths and col_path not in prio_paths:
            for p in prio_paths:
                if p not in forbidden and _is_column_public_now(p, now_jst=now_jst) and not _is_column_used_today(p, date_str):
                    col_title, col_path = _title_for_column_path(p), p
                    break

    if col_path in forbidden or _is_column_used_today(col_path, date_str):
        for fb_title, fb_path in _FALLBACK_COLUMNS:
            if fb_path not in forbidden and not _is_column_used_today(fb_path, date_str):
                col_title, col_path = fb_title, fb_path
                break

    return col_title, col_path


def get_tip_reply_text(slot_label: str = "12:00", tip_index: int = None, now_jst: datetime.datetime = None) -> str:
    """Build the 2nd post (self-reply) with the matching column URL + PDCA tracking tag."""
    site_url = os.environ.get("PUBLIC_SITE_URL", "https://aiseki-navi.com").rstrip("/")
    slot_hh = (slot_label or "12:00")[:2]
    col_title, col_path = resolve_tip_column(slot_label=slot_label, tip_index=tip_index, now_jst=now_jst)

    return (
        f"📖 詳しい解説コラム：{col_title}👇\n"
        f"{site_url}{col_path}?s=x{slot_hh}_tip"
    )


def _generate_ai_daily_human_tip(
    seed_tip_text: str,
    slot_label: str,
    standalone_no_reply: bool,
    col_title: str,
    now_jst: datetime.datetime,
) -> str:
    """
    Re-express today's curated lounge observation (`seed_tip_text`) as a fresh, never-repeated
    human insider monologue tuned to tonight's actual weather, day-of-week, and calendar mood.
    Returns "" if AI is unconfigured or output exceeds strict X hygiene constraints.
    """
    try:
        import re
        import sns_editorial
        if not sns_editorial.has_any_ai_key(allow_blocking=True):
            return ""
        weather_ctx = sns_editorial.fetch_live_weather_context(allow_blocking=True) or {}
        cal_ctx = sns_editorial.build_calendar_context(now_jst)
        ctx_info = {
            "slot": slot_label,
            "weekday": f"{cal_ctx.get('weekday_ja', '')}曜",
            "calendar_vibe": f"{cal_ctx.get('tag', '')}（{cal_ctx.get('note', '')}）",
            "weather": weather_ctx.get("adlib_hint") or weather_ctx.get("summary") or "",
            "theme_seed": seed_tip_text,
            "has_reply_column": (not standalone_no_reply),
            "reply_column_title": col_title if not standalone_no_reply else "",
        }
        ending_rule = (
            "今日はリプ欄なしの完全1ツイート独り言の日なので、『👇』や『リプ欄』への誘導は書かず、ふと呟いた余韻と末尾の『#相席ラウンジ』1個だけで締めること。"
            if standalone_no_reply
            else "最後は宣伝臭くならない自然な一言でリプ欄の深掘りメモへ流すか、余韻を残しつつ末尾に『#相席ラウンジ #相席ナビ』を置くこと。"
        )
        prompt = (
            "あなたは全国53店舗の相席ラウンジを見続けている『相席ナビ』の運営中の人です。\n"
            "夕方18時頃、これから飲みに出る人たちへ向けてXでふと呟いた『人間味100%のリアル観察・本音ポスト（88〜124文字以内、X換算262ウェイト以下）』を1本書いてください。\n\n"
            f"{sns_editorial.HUMAN_INSIDER_VOICE_GUIDE}\n\n"
            "【必須ルール】\n"
            "1. 下の `theme_seed`（今日のテーマ原案）の核心となる気づき・毒・女性目線の図星を活かしつつ、今日の曜日・天気・季節の空気感をほんの少し絡めて、過去と一字一句被らない新しい生の口語ツイートに書き直すこと。\n"
            "2. URL（http...）や架空の店舗人数、角括弧【】見出しは絶対に入れないこと。単体の『オリエンタル』表記も禁止。\n"
            f"3. {ending_rule}\n\n"
            f"【今日のコンテキスト】\n{sns_editorial.json.dumps(ctx_info, ensure_ascii=False)}\n\n"
            '出力はJSON形式のみ：{"thinking_memo": "どう定型感を消して生の独り言にしたか", "main_text": "投稿本文"}'
        )
        parsed, _, _ = sns_editorial.call_editorial_ai_json(
            prompt=prompt,
            max_output_tokens=1800,
            thinking_budget=1024,
            total_deadline_sec=28.0,
            per_call_timeout_sec=19.0,
            now_jst=now_jst,
            prefer_fast=True,
        )
        if not parsed or not parsed.get("main_text"):
            return ""
        txt = sns_editorial._postprocess_ai_main_text(parsed["main_text"], channel="x")
        if "http://" in txt or "https://" in txt:
            return ""
        if standalone_no_reply:
            txt = txt.replace("👇", "").replace("リプ欄", "")
        wt = x_poster.compute_twitter_weight(txt)
        if 105 <= wt <= 272 and not any(w in txt for w in sns_editorial.AI_CLICHE_WORDS):
            return txt
    except Exception as e:
        logger.warning(f"Tip AI generation fallback: {e}")
    return ""


def run_scheduled_tip_post(slot_label: str = "12:00", force: bool = False, tip_index: int = None, now_jst: datetime.datetime = None) -> dict:
    """
    Execute daily tip posting to X (Twitter).
    slot_label: '12:00' or '18:00'
    Posts a clean URL-free main post; on thread days attaches a reply with the column URL,
    and on alternating 18:00 days posts a pure 1-tweet observation with zero external links.
    """
    now_jst = now_jst or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    date_str = now_jst.strftime("%Y-%m-%d")
    dedup_key = f"x_tip:{date_str}_{slot_label}"
    orig_tip_index = tip_index

    # 投稿済みフラグは job_state（Google Sheets 永続化）で管理し、再起動・再デプロイ後の二重投稿を防ぐ
    import job_state
    if not force and job_state.is_done(dedup_key):
        logger.info(f"X tip post already sent for {dedup_key}, skipping.")
        return {"status": "skipped", "reason": "Already posted for this slot today"}

    standalone = is_standalone_url_free_tip(slot_label=slot_label, now_jst=now_jst, tip_index=orig_tip_index) and not force
    idx = _select_tip_index(slot_label=slot_label, tip_index=tip_index, now_jst=now_jst)
    fallback_text = get_tip_for_slot(slot_label=slot_label, tip_index=idx, now_jst=now_jst, standalone_no_reply=standalone)
    if not fallback_text:
        return {"status": "error", "detail": "Empty tip text"}

    col_title = ""
    col_path = ""
    reply_text = None
    if not standalone:
        col_title, col_path = resolve_tip_column(
            slot_label=slot_label,
            tip_index=idx,
            now_jst=now_jst,
            prefer_pdca=(orig_tip_index is None and slot_label != "12:00"),
        )
        site_url = os.environ.get("PUBLIC_SITE_URL", "https://aiseki-navi.com").rstrip("/")
        slot_hh = (slot_label or "12:00")[:2]
        reply_text = f"📖 現場ログからの深掘りメモ：{col_title}👇\n{site_url}{col_path}?s=x{slot_hh}_tip"

    raw_seed = TIPS_LIST[idx % len(TIPS_LIST)]
    ai_text = _generate_ai_daily_human_tip(
        seed_tip_text=raw_seed,
        slot_label=slot_label,
        standalone_no_reply=standalone,
        col_title=col_title,
        now_jst=now_jst,
    )
    text = ai_text or fallback_text

    if force:
        suffix = f"\n(⏱️{now_jst.strftime('%H:%M')})"
        if x_poster.compute_twitter_weight(text + suffix) <= 278:
            text = text + suffix

    result = x_poster.post_to_x(text, reply_text=reply_text)
    if result.get("status") == "success" and not force:
        job_state.mark_done(dedup_key)
        x_poster.record_x_auto_post_timestamp(now_jst)
        if col_path:
            job_state.mark_done(f"x_col_used:{date_str}:{col_path}")

    result["tweet_text"] = text
    result["reply_text"] = reply_text or ""
    result["column_path"] = col_path
    result["standalone_no_reply"] = standalone
    result["slot"] = slot_label
    return result
