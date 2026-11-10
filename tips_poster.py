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

# 71 human-toned observations blending street realities and empirical lounge data.
# Each tip is sized so that the tip + footer + hashtag stays strictly within X API's 280-weight limit.
TIPS_LIST = [
    # --- 1. 会話・空気感・乾杯10分のリアル (idx 0..9 -> /guide/first-10-minutes) ---
    (
        "相席して最初の3分で「休みの日なにしてる？」「どこ住み？」と履歴書を確かめ始めると、向かいの表情が少しだけ固くなる。\n"
        "「1軒目どのへんで飲んでたの？」くらいの雑談から入った席のほうが、空気のほどけ方が早い。"
    ),
    (
        "相席で空気が一番冷えるのは、聞かれてもいない仕事の武勇伝と忙しさの話。\n"
        "向こうも仕事終わりに息抜きで来ている。すごく見せようとする人より、「それわかる」と一緒に笑える隙のある人に会話は寄っていく。"
    ),
    (
        "テーブルのスマホが画面を上にしたまま、通知をちらちら確認され始めたら、その席は少し退屈されている。\n"
        "居心地のいい席ほどスマホはバッグの中か裏返しのまま。質問を止めて、食べ物の話に戻すくらいがちょうどいい。"
    ),
    (
        "「何歳に見える？」に、とりあえずマイナス3歳で返すと、だいたい社交辞令として流される。\n"
        "「落ち着いてるから同い年かと思ったけど、笑うと年下っぽい」くらいの温度差を添えると、そこから話が一段ゆるむ。"
    ),
    (
        "「女の子はタダで飲めていいよな」は、冗談のつもりでもその場で静かに距離を置かれる一言。\n"
        "向こうは1時間半かけてメイクして髪を巻き、ヒールで来ている。その時間と準備を想像できる人ほど、席で信頼される。"
    ),
    (
        "盛り上がらない席の9割は、気づかないうちに面接官の一問一答になっている。\n"
        "答えが返ってきたらすぐ次の質問へ行かず、「いいなそれ、実は俺もさ」と自分の少し抜けた話をひとつ挟む。それだけで会話が転がり出す。"
    ),
    (
        "「趣味は？」と聞かれると身構える人でも、「最近ハマってるものある？ネトフリでも食べ物でも」なら案外すらすら話してくれる。\n"
        "そこから「今度そこ行ってみたい」へ繋げると、無理のない流れになる。"
    ),
    (
        "乾杯して最初の5分で、ネイルやピアスの色、髪の巻きのツヤにさらっと気づける人は印象に残る。\n"
        "顔を大げさに褒められると構えてしまうけど、準備に時間をかけたディテールに触れられて嫌な気はしないものだったりする。"
    ),
    (
        "乾杯から10分で沈黙が落ちたら、無理にウケを狙うより「ここのフード、何が当たりか聞いてみる？」と店員さんを巻き込むのが手。\n"
        "メニュー選びを一緒にやるだけで、初対面の硬さが一度リセットされる。"
    ),
    (
        "自己紹介で肩書きをきっちり語るほど、「いい人だけど堅そう」で印象が止まりがち。\n"
        "「〇〇系の仕事で、最近は仕事終わりにサウナか町中華ばっかり開拓してる」くらい生活感が見えると、相手もツッコミを入れやすい。"
    ),

    # --- 2. 距離70cmの身だしなみ・女性目線のシビア査定 (idx 10..19 -> /grooming) ---
    (
        "ラウンジの照明は暗くても、ソファ席の距離70cmだと夕方の肌のテカリや唇の乾燥は意外とはっきり見えている。\n"
        "エレベーターに乗る前、汗拭きシートとリップを一度使うだけで、乾杯の第一印象はかなり変わる。"
    ),
    (
        "女子トイレの反省会で意外と名前が挙がるのが、「ジャケットはきれいなのに靴がくたびれている」パターン。\n"
        "ソファ席に案内されるときも席を立つときも、足元は思った以上に目に入る。出る前に靴の汚れだけは見ておきたい。"
    ),
    (
        "狭いボックス席に強めの香水で来ると、料理の香りとぶつかって1杯目で席替えになることも少なくない。\n"
        "70cmの距離で心地いいのは、柔軟剤かヘアバームがふわっと香る程度。香りは近づいて気づくくらいで十分。"
    ),
    (
        "気合いを入れて全身ロゴだらけで来ると、向かいからは「遊んでそう」「圧が強そう」と少し身構えられやすい。\n"
        "結局信頼されるのは、シワのない無地のニットやシャツをジャストサイズで着ている、余裕のある普通の人。"
    ),
    (
        "間接照明の店内で前髪が重いと、目元に影が落ちて実物より3割くらい暗い人に見える。\n"
        "眉のボサつきを整えて、おでこを少し出す。それだけで乾杯の瞬間に「爽やかな人だな」という印象が残りやすい。"
    ),
    (
        "1軒目でニンニク系やタバコを挟んでそのまま来ると、本人は気づかなくても距離70cmでは確実に伝わっている。\n"
        "どれだけ話が面白くても、口元の匂いひとつで印象はひっくり返る。入店前のブレスケアはケチらないほうがいい。"
    ),
    (
        "タッチパネルで注文するとき、グラスを持つとき、視線が自然と向かうのは指先と袖口。\n"
        "爪が伸びていたり袖が汚れていたりすると、それだけで気持ちが引いてしまう人は多い。顔立ちより手入れの跡のほうが、ずっと印象に残る。"
    ),
    (
        "インスタやLINEを交換するとき、差し出したスマホの画面がバキバキだったり指紋でベタついていたりすると、その瞬間ちょっと熱が下がる。\n"
        "入店前にさっと拭いておく。意外と見られているのがスマホの清潔感だったりする。"
    ),
    (
        "服の値段はほとんど見られていないのに、ニットの毛玉と首元のヨレは3秒で気づかれている。\n"
        "ユニクロで全然かまわないので、くたびれていない服を着ていく。それが向かいに座る人への最低限の礼儀になる。"
    ),
    (
        "「清潔感って結局顔でしょ」と思われがちだけど、見られているのは隣を歩いて気にならないサイズ感と肌のツヤくらい。\n"
        "ダボつかない服と整った眉と髪。それだけで上位2割の印象には十分入れる。"
    ),

    # --- 3. 2人組ペアの立ち回り・女子トイレ会議の裏側 (idx 20..27 -> /guide/solo-vs-pair) ---
    (
        "男2人で対面ソファに座るとき、気になる相手の真正面を取ると、目線の逃げ場がなくて一問一答になりやすい。\n"
        "あえて斜め向かいに座ると視線を自然に外せて、2人だけの会話も生まれやすくなる。"
    ),
    (
        "相席の流れは、正直「誰と組んで行くか」で7割決まる。\n"
        "2人とも自分が話したいペアも、2人とも待ちの姿勢のペアも苦戦しやすい。片方がパスを出し、片方が広げられるコンビは、どの席でも空気を作るのがうまい。"
    ),
    (
        "場を盛り上げようとして一緒に来た友達をイジって下げると、向かいからは「性格きつそう」と2人まとめて見られてしまう。\n"
        "「こいつ仕事ほんと頼りになるんだよ」と相方を自然に立てられるペアのほうが、ずっと好印象を残す。"
    ),
    (
        "女子2人が乾杯から15分ほどで「ちょっとお手洗い行こっか」と同時に立ったら、鏡の前で「この席どうする？」の作戦会議中だったりする。\n"
        "戻ってから食事のペースが急に上がったら、そろそろ切り上げる準備のサイン。"
    ),
    (
        "4人で飲んでいるのに、片方の相手を空気のように扱う人はまずうまくいかない。\n"
        "ペアで来た2人は運命共同体。友達が退屈した時点で「明日早いし帰ろっか」の流れができる。4人全員を楽しませられる人が、結局最後まで残る。"
    ),
    (
        "相方が気になる相手と良い空気になっていたら、もう1人を全力で楽しませるアシストに回れるかどうか。\n"
        "友達側の満足度が高いと、「もう少し4人で2軒目行こう」と向こうから背中を押してもらえることも多い。"
    ),
    (
        "入店前に相方と「微妙なら40分で店を変える」「当たったら裏のバーに誘う」の2行だけ決めておく。\n"
        "お会計前に目配せでもたつかないペアは、向かいから見てもこなれていて余裕があるように映る。"
    ),
    (
        "男2人で会社の愚痴や大学時代の内輪ネタを始めた瞬間、向かいの2人は静かに「とりあえず食べて帰るモード」に切り替わる。\n"
        "初対面の4人全員が乗れる話題かどうか。それを意識するだけで、席の寿命はぜんぜん違ってくる。"
    ),

    # --- 4. 連絡先交換・グループLINE・2軒目誘導 (idx 28..35 -> /guide/second-venue) ---
    (
        "お会計直前の「4人でグループLINE作ろう」は誰も傷つかない反面、9割が翌日には通知オフのまま眠っていく。\n"
        "会話が一番盛り上がった瞬間に「さっきのお店、インスタのDMで送るね」と個別で繋がったほうが、次に続きやすい。"
    ),
    (
        "2軒目の誘いが「もう1軒飲みに行こう」だと、長くなりそうで重く聞こえて断られやすい。\n"
        "「すぐ裏に夜パフェとおいしいコーヒーのバーがあるから、30分だけ寄らない？」と甘いもの×短時間で区切ると、すっと通る。"
    ),
    (
        "居心地がいいからと店内で2時間粘るのは、いちばんもったいない流れ。\n"
        "23時を過ぎると終電とメイク崩れが気になり始める人が多い。余韻が残る45〜50分目あたりで、さっと外の空気へ連れ出すくらいがちょうどいい。"
    ),
    (
        "解散後の1通目は、家に着いてからの長文より、改札で別れて5分以内の1行のほうが返ってきやすい。\n"
        "帰りの電車でスマホを開いたタイミングに「今日めっちゃ笑った、気をつけて帰って」と届く。それくらいの軽さが一番残る。"
    ),
    (
        "2軒目を断られた瞬間にあからさまにテンションが下がると、交換したばかりの連絡先までその場で温度を失う。\n"
        "「明日早いもんね、今日乾杯できて楽しかった」と爽やかに解散できる人ほど、翌週のサシ飯に繋がっている。"
    ),
    (
        "交換した直後、意外と真っ先に見られているのがLINEのアイコンと名前。\n"
        "キメた自撮りや加工の強い写真より、友達が撮った自然な笑顔や旅先、おいしかったご飯の写真のほうが、受け取る側の安心感はずっと大きい。"
    ),
    (
        "せっかく「2軒目行こっか」となったのに、路上で食べログを検索し始めると、さっきまでの空気が冷めていく。\n"
        "よく行く街なら、ラウンジから徒歩3分以内の静かなバーか夜カフェを1軒だけ決めておくと迷わない。"
    ),
    (
        "いきなりLINEを聞くより、「インスタ交換しよう」のほうが今の空気に合っていて構えられにくい。\n"
        "普段の投稿やストーリーでお互いの雰囲気がわかるし、数日後にグルメ投稿へ軽く反応するところから、自然に次の約束へ繋がっていく。"
    ),

    # --- 5. お店のシステム・ブランド別の本音使い分け (idx 36..39) ---
    (
        "噛み合わない席で気を遣って60分、90分と延ばしても、向かいの二人も実はそっと時計を見ていることが多い。\n"
        "合わないと感じたら35〜40分で笑って切り上げる。それがお互いの夜にとって一番やさしい。"
    ),
    (
        "相席ラウンジの体感コスパは、曜日と時間帯、イベントの有無でまるで別物になる。\n"
        "公式アプリのチャージ優待に加えて、今どの店に人が集まっているかを入る前に見ておくだけで、待ちに払う時間とお金はほとんど消せる。"
    ),
    (
        "オリエンタルラウンジ、ag、JIS。看板ごとの空気を知らずに入ると、意外なほどすれ違う。\n"
        "落ち着いて飲む夜はオリエンタルラウンジ、20代前半のノリならag、サクッと回すならJIS。この使い分けで十分。"
    ),
    (
        "乾杯より前に、案外みんな見ているのが店員さんへの態度。\n"
        "案内やドリンクを受け取るときのタメ口や横柄さは、見た目や会話がどれだけ良くても、その一瞬で静かに印象を決めてしまう。"
    ),

    # --- 6. 曜日・時間帯・街別のリアルなギャップ (idx 40..49) ---
    (
        "新宿や渋谷のように徒歩5分圏内に何店舗もある街で、最初の店の「男性10組待ち」に意地で並ぶのはもったいない。\n"
        "目と鼻の先の別ブランドを覗いたら女性が多くて即着席、という夜は珍しくない。"
    ),
    (
        "金曜22時のピークに飛び込んで受付で消耗するより、水曜・木曜の20時半のほうが男性の列は短く、落ち着いて話せる相手にも当たりやすい。\n"
        "慣れている人ほど、週末のど真ん中をあえて外している。"
    ),
    (
        "金土にどうしても行くなら、1軒目帰りが押し寄せる21時半より前、19:45〜20:30に先に座っておくのが楽。\n"
        "後から入ってくる女性グループを、焦りのない状態で迎えられる。"
    ),
    (
        "意外と落ち着いているのが日曜の20時〜22時。\n"
        "月曜休みの美容師、アパレル、医療系の人がふらっと飲みに来ていることが多く、金土のざわつきがないぶん、会話の密度がぐっと上がる。"
    ),
    (
        "雨の夜、傘をさして路面店まで歩くと前髪も靴も崩れる。だから女性の足は地下街や駅直結の店に向きやすい。\n"
        "しかも雨は男性の出足も鈍るので、駅直結の店だけ女性比率が跳ね上がる夜になったりする。"
    ),
    (
        "終電後の0時半を回ると、軽く一杯だけの層が帰り、まだ話し足りない人だけが残る。\n"
        "男性の待ち列も消える時間帯なので、深夜に強い店舗をいくつか知っておくと、2軒目・3軒目の選択肢がかなり広がる。"
    ),
    (
        "エレベーターが開いた瞬間、スーツ姿の男性ばかりで肩を落とすあの感じ。向かう前の10秒チェックでほぼ防げる。\n"
        "勢いで突っ込むより、リアルタイムの男女人数を見て、今ちょうどいい店だけを選ぶほうがずっとスマート。"
    ),
    (
        "新作スイーツや美容ギフトのイベントがある夜は、女性側にとって友達を誘う口実になりやすい。\n"
        "店選びで迷ったら、その日に女性向けキャンペーンが走っている店から見ていくと、早い時間から席が動きやすい。"
    ),
    (
        "出張で札幌・仙台・名古屋・福岡・熊本あたりに泊まる夜、ホテルで一人飲みするより、地元のラウンジを覗くと思いのほか話が弾む。\n"
        "「出張で来ていて、この街のおいしい店を知りたくて」は、それだけで自然な会話の入り口になる。"
    ),
    (
        "同じ新宿でも、ある店は男性余りでピリついているのに、徒歩4分の別の店は女性比率65%で穏やか、という差が今この瞬間に起きている。\n"
        "店の知名度より、その時の比率を見る癖がつくと夜の選び方が変わる。"
    ),

    # --- 7. メンタル・余裕・男女の非対称性 (idx 50..52 -> /guide) ---
    (
        "10分ごとに料金が加算されるから早く結果を、という焦りは、向かいの席に驚くほど伝わる。\n"
        "ガツガツ口説くより、今日楽しく飲めたら十分くらいの余白がある人のほうが、2軒目の誘いにも自然に頷いてもらえている。"
    ),
    (
        "お酒が弱そうな人や、2軒目で少し眠そうな人に「もっと飲もう」と煽るのは避けたい。\n"
        "「お水か温かいお茶、頼みましょうか」と先にペースを気遣える人のほうが、信頼はずっと厚くなる。"
    ),
    (
        "女性2人組で行くなら、女性がたくさん入っている人気店はかえって待たされやすく、30分ほど手持ち無沙汰になることもある。\n"
        "男性が先に多く入っている大型店を選んだほうが、待ち0分ですっと席につけて気がラク。"
    ),

    # --- 8. 美容サロン激戦区・深夜第2波・実測データの法則 (idx 53..70) ---
    (
        "土日の18時半〜20時頃、茶屋町・渋谷・新宿・心斎橋のラウンジが華やかなのは、周辺の美容サロン帰りと時間が重なるから。\n"
        "髪もネイルも整えた日に、まっすぐ帰りたくない1軒目の人が入ってくる早い時間帯は狙い目になる。"
    ),
    (
        "サロン帰りで髪がつややかな人や、ネイルを替えたばかりの人に、乾杯3分で「美容院帰りですか、その色きれいですね」と気づける人は強い。\n"
        "見た目そのものより、その日かけた手間に目が向くのが大人の余裕だったりする。"
    ),
    (
        "渋谷・恵比寿・茶屋町・新宿・栄のように美容意識の高い人が集まるエリアほど、清潔感の見られ方もシビアになる。\n"
        "70cmの距離だと眉や肌のテカリ、服のヨレはごまかせない。入店前の3分の身だしなみがそのまま印象に出る。"
    ),
    (
        "梅田・心斎橋・渋谷・新宿のように大型クラブが近い街では、終電後の0時半〜1時半にもう一度女性が増える深夜の第2波がある。\n"
        "終電で終わりと決めつけず、深夜まで比率が保たれる店を知っておくと、2軒目の組み立てがかなり楽になる。"
    ),
    (
        "「女性25人・男性2人」のような極端な数字を見て向かうと、着く頃には半分以上いなくなっていることがある。\n"
        "女性側は待たされる店を30分で見切って移るので、男性もほどよくいて女性が増え始めた店が一番座りやすい。"
    ),
    (
        "東京の感覚で熊本・沖縄・金沢・仙台のラウンジに20時に行くと静かだが、深夜0時〜2時には女性が121%まで逆転して増えることがある。\n"
        "地方は居酒屋帰りの2軒目が1店舗に集まりやすい。出張先なら22時半以降が本番になる。"
    ),
    (
        "相席で財布も気持ちも削られるのは、盛り上がりきらない席で気を遣って90分粘り、最後に1万円近いレシートを見て苦笑いする夜。\n"
        "40分を区切りにして、弾めば外のバーへ、噛み合わなければバランスのいい店へ移るほうがずっと賢い。"
    ),
    (
        "25日の給料日直後は気合いの入った男性が集まって待ち列が伸びる。一方、15〜24日の給料日前は男性が減り、女性比率が平均+7.4%上がる。\n"
        "人が動かない雨の地下直結店や給料日前こそ、いちばん落ち着いて座れる日だったりする。"
    ),
    (
        "1軒目が23時すぎに終わり、23時20分〜23時55分の40分間に飛び込むと、終電前の退店ラッシュで空振りしやすい。\n"
        "夜カフェで30分つないで、0時30分の第2波が来てからオリエンタルラウンジやJISへ入るほうが、流れに素直に乗れる。"
    ),
    (
        "梅田の東通りが男性40分待ちの夜は、立ち尽くすより徒歩6分の茶屋町か、御堂筋線で9分の難波を見たほうが早い。\n"
        "20時台はJIS茶屋町、22時台は裏天満やオリエンタルラウンジ難波と、時間帯で主役が入れ替わる街でもある。"
    ),
    (
        "金曜22時に女性比60%を見て向かったのに、着いたら男性余りになっていた、という夜は珍しくない。\n"
        "22時台は1次会帰りの男性が一斉に動くので、約3割が到着時に逆転する。この時間以降は女性比70%超の店を選んでおきたい。"
    ),
    (
        "会話が止まった席で、席替えは失礼かと迷う人は多い。でも向かいの二人も、内心そのタイミングを待っていたりする。\n"
        "「明日早いですよね、そろそろ交代お願いしましょうか」と笑って切り出せる人は、むしろ感じよく記憶に残る。"
    ),
    (
        "20時は女性8割だった店が、深夜0時には男性先行にひっくり返ることは結構ある。\n"
        "男性なら21時前が余裕を持てる時間だが、女性2人組にとっては深夜0時すぎの男性先行フロアこそ、待ちゼロですぐ乾杯できる時間帯になる。"
    ),
    (
        "同じ街でも、オリエンタルラウンジは終電後まで落ち着いた比率が残りやすく、agやヤタコイは0時すぎに2軒目の男性が増えて、女性の待ちがほぼなくなる。\n"
        "店の優劣ではなく、今夜の時間帯とノリの相性で選べば外しにくい。"
    ),
    (
        "男性から見れば当たりに思える女性80%の店は、女性2人組からすると相席がつかず待たされやすい店でもある。\n"
        "男女で「いま入るべき店」が真逆になる。夜の街のいちばん面白いところはそこにある。"
    ),
    (
        "相席ラウンジは20代前半ばかりで、アラサーは浮くのでは、と迷う人は多い。実際はagとオリエンタルラウンジやJISでは客層がまったく違う。\n"
        "学生街を避けて平日の20時に入ると、落ち着いた同世代の社会人と自然に会話が弾みやすい。"
    ),
    (
        "友達と2人で乾杯したい金曜20時台は相席ラウンジが早い。1対1でじっくり話すのが得意なら、週末は審査制のカフェデートと使い分けると自然体でいられる。\n"
        "得意な場所を2つ持っておくと、夜の余裕がまるで違ってくる。"
    ),
    (
        "暗いからヒゲやテカリは目立たない、と思いがちだが、テーブル真上のスポット照明と70cmの距離は昼間より陰影が出る。\n"
        "エレベーターに乗る前、洗面所でテカリを押さえて襟元を整える。その3分で印象はまるで変わる。"
    ),
    (
        "日曜の相席ラウンジで一番避けたいのは、21時過ぎに金土のノリで二次会へ引っ張ること。\n"
        "翌朝9時出勤の女性にとって21時半は防衛ライン。21時15分に「明日早いよね？楽しかったからまた行こ」と男側から引ける人が次回に繋がります。"
    ),
    (
        "相席ラウンジでLINE交換した後の既読スルーを防ぐ鍵は「退店直後30分以内」の個別連絡。\n"
        "女性は1晩に何組もの男性と話しており翌朝には記憶が薄れています。席で話した具体的エピソードや写真・URLを1枚添えることでトーク一覧での埋没を防げます。"
    ),
]


_FALLBACK_COLUMNS = [
    ("相席ラウンジのLINE交換、なぜ翌日既読スルーされるのか？｜女性の記憶容量と連絡の境界線", "/guide/line-exchange-reality"),
    ("日曜日の相席ラウンジは行くべきか？｜21時半の撤退急落と、月曜休みの隠れ狙い目", "/guide/sunday-lounge-strategy"),
    ("相席ラウンジと審査制1対1デートの使い分け｜実測ログで見る境界線", "/guide/lounge-vs-screened-date"),
    ("相席ラウンジの服装と清潔感チェック｜避けたいドレスコードと70cm照明対策", "/guide/dress-code-cleanliness"),
    ("相席ラウンジは30代でも浮かない？｜店舗選びと客層データから見る境界線", "/guide/thirties-lounge-guide"),
    ("渋谷・恵比寿5店舗の年齢層・時間波形・1駅ハシゴ比較", "/guide/shibuya-ebisu-comparison"),
    ("上野・錦糸町・町田・柏5店舗の沿線ターミナル比較", "/guide/ueno-kinshicho-machida"),
    ("横浜・大宮・宇都宮・高崎5店舗の時間波形・料金比較", "/guide/yokohama-omiya-kanto"),
    ("名古屋（栄・錦・住吉）5店舗の年齢層・ソロ境界比較", "/guide/nagoya-sakae-nishiki"),
    ("福岡・天神と九州・沖縄11店舗のブランド使い分け", "/guide/fukuoka-tenjin-kyushu"),
    ("広島・岡山・松山6店舗の3店競合と料金システム比較", "/guide/hiroshima-okayama-matsuyama"),
    ("札幌すすきの・仙台国分町の時間波形と曜日ルール", "/guide/sapporo-sendai-north"),
    ("20時と深夜0時で「主役」は入れ替わる｜5大ブランドの時間波形と男女逆転の店選び", "/guide/brand-waveform-asymmetry"),
    ("相席ラウンジの席替えの頼み方｜言い出せない男性と察している女性の心理", "/guide/table-change-protocol"),
    ("金土22時の「女性比60%」が3割逆転する理由｜群衆殺到のパラドックス", "/guide/crowd-rush-paradox"),
    ("全国60店舗超「料金・イベント・現在人数」比較ボードの使い方", "/guide/price-event-comparison"),
    ("最初の10分で差がつく会話と清潔感", "/guide/first-10-minutes"),
    ("相席ラウンジ前の身だしなみ・清潔感チェック", "/grooming"),
    ("1人（ソロ）と2人ペア、立ち回りの違い", "/guide/solo-vs-pair"),
    ("2軒目・カフェやバーへの自然な誘い方", "/guide/second-venue"),
    ("オリエンタルラウンジ・ag・JISの違いと選び方", "/guide/brand-comparison"),
    ("水曜・木曜の20時〜22時が穴場になる理由", "/guide/weekday-strategy"),
    ("金曜・土曜ピークの待ち時間を避けるハシゴの組み方", "/guide/weekend-strategy"),
    ("新宿エリア3店舗の使い分けと穴場", "/guide/shinjuku"),
    ("美容サロンが集まる街の相席ラウンジは、なぜ華やかなのか", "/guide/beauty-area-stage"),
    ("ダンスクラブ街と深夜2回転目の回遊パターン", "/guide/club-area-midnight"),
    ("「男性先行」の店ほど女性が増える？女性は待たないという実測", "/guide/male-lead-mechanics"),
    ("相席ラウンジの基本ガイド（立ち回り・お店選び）", "/guide"),
    ("全国60店舗超 曜日・時間帯別の統計データ", "/stats"),
    ("相席ラウンジ実践コラム・記事一覧", "/columns"),
]


def _column_for_tip_index(idx: int) -> tuple:
    """Return the primary (col_title, col_path) best matching TIPS_LIST[idx]."""
    if idx <= 9:
        return ("最初の10分で差がつく会話と清潔感", "/guide/first-10-minutes")
    if idx <= 19:
        return ("相席ラウンジ前の身だしなみ・清潔感チェック", "/grooming")
    if idx <= 27:
        return ("1人（ソロ）と2人ペア、立ち回りの違い", "/guide/solo-vs-pair")
    if idx <= 35:
        return ("2軒目・カフェやバーへの自然な誘い方", "/guide/second-venue")
    if idx == 37:
        return ("全国60店舗超「料金・イベント・現在人数」比較ボードの使い方", "/guide/price-event-comparison")
    if idx <= 39:
        return ("オリエンタルラウンジ・ag・JISの違いと選び方", "/guide/brand-comparison")
    if idx == 40:
        return ("新宿エリア3店舗の使い分けと穴場", "/guide/shinjuku")
    if idx == 41:
        return ("水曜・木曜の20時〜22時が穴場になる理由", "/guide/weekday-strategy")
    if idx == 42:
        return ("金曜・土曜ピークの待ち時間を避けるハシゴの組み方", "/guide/weekend-strategy")
    if idx == 43:
        return ("水曜・木曜の20時〜22時が穴場になる理由", "/guide/weekday-strategy")
    if idx == 44:
        import scheduled_articles
        if scheduled_articles.is_published("rain-payday"):
            return ("雨の日・給料日前・連休中日｜9.6万件ログで見る穴場日の傾向", "/guide/rain-payday")
        return ("金曜・土曜ピークの待ち時間を避けるハシゴの組み方", "/guide/weekend-strategy")
    if idx == 45:
        return ("ダンスクラブ街と深夜2回転目の回遊パターン", "/guide/club-area-midnight")
    if idx == 46:
        return ("全国60店舗超 曜日・時間帯別の統計データ", "/stats")
    if idx == 47:
        return ("全国60店舗超「料金・イベント・現在人数」比較ボードの使い方", "/guide/price-event-comparison")
    if idx == 48:
        import scheduled_articles
        if scheduled_articles.is_published("local-vs-urban"):
            return ("地方店と都心大型店｜深夜0時に女性が121%へ増える理由", "/guide/local-vs-urban")
        return ("全国エリア別 リアルタイム混雑＆店舗ガイド", "/areas")
    if idx == 49:
        return ("新宿エリア3店舗の使い分けと穴場", "/guide/shinjuku")
    if idx in (50, 51, 52):
        return ("相席ラウンジの基本ガイド（立ち回り・お店選び）", "/guide")
    if idx in (53, 54, 55):
        return ("美容サロンが集まる街の相席ラウンジは、なぜ華やかなのか", "/guide/beauty-area-stage")
    if idx == 56:
        return ("ダンスクラブ街と深夜2回転目の回遊パターン", "/guide/club-area-midnight")
    if idx == 57:
        return ("「男性先行」の店ほど女性が増える？女性は待たないという実測", "/guide/male-lead-mechanics")
    if idx == 58:
        import scheduled_articles
        if scheduled_articles.is_published("local-vs-urban"):
            return ("地方店と都心大型店｜深夜0時に女性が121%へ増える理由", "/guide/local-vs-urban")
        return ("相席ラウンジ実践コラム・記事一覧", "/columns")
    if idx == 59:
        import scheduled_articles
        if scheduled_articles.is_published("cost-break-even"):
            return ("90分粘るか、40分で切り上げるか｜滞在時間と料金の損益分岐点", "/guide/cost-break-even")
        return ("相席ラウンジ実践コラム・記事一覧", "/columns")
    if idx == 60:
        import scheduled_articles
        if scheduled_articles.is_published("rain-payday"):
            return ("雨の日・給料日前・連休中日｜9.6万件ログで見る穴場日の傾向", "/guide/rain-payday")
        return ("相席ラウンジ実践コラム・記事一覧", "/columns")
    if idx == 61:
        return ("23時20分、店内の空気が変わる｜9.6万件のログで見えた「40分間」と街ごとのハシゴの流れ", "/guide/last-train-cliff")
    if idx == 62:
        import scheduled_articles
        if scheduled_articles.is_published("osaka-kita-vs-minami"):
            return ("梅田キタと難波ミナミ｜大阪全9店舗の南北ハシゴ", "/guide/osaka-kita-vs-minami")
        return ("大阪・梅田エリアのリアルタイム混雑・店舗ガイド", "/area/umeda")
    if idx == 63:
        return ("金土22時の「女性比60%」が3割逆転する理由｜群衆殺到のパラドックス", "/guide/crowd-rush-paradox")
    if idx == 64:
        import scheduled_articles
        if scheduled_articles.is_published("table-change-protocol"):
            return ("相席ラウンジの席替えの頼み方｜言い出せない男性と察している女性の心理", "/guide/table-change-protocol")
        return ("相席ラウンジ実践コラム・記事一覧", "/columns")
    if idx in (65, 66, 67):
        return ("20時と深夜0時で「主役」は入れ替わる｜5大ブランドの時間波形と男女逆転の店選び", "/guide/brand-waveform-asymmetry")
    if idx == 68:
        import scheduled_articles
        if scheduled_articles.is_published("thirties-lounge-guide"):
            return ("相席ラウンジは30代でも浮かない？｜店舗選びと客層データから見る境界線", "/guide/thirties-lounge-guide")
        return ("相席ラウンジ実践コラム・記事一覧", "/columns")
    if idx == 69:
        return ("相席ラウンジと審査制1対1デートの使い分け｜実測ログで見る境界線", "/guide/lounge-vs-screened-date")
    if idx == 70:
        return ("相席ラウンジの服装と清潔感チェック｜避けたいドレスコードと70cm照明対策", "/guide/dress-code-cleanliness")
    if idx == 71:
        import scheduled_articles
        if scheduled_articles.is_published("sunday-lounge-strategy"):
            return ("日曜日の相席ラウンジは行くべきか？｜21時半の撤退急落と、月曜休みの隠れ狙い目", "/guide/sunday-lounge-strategy")
        return ("相席ラウンジ実践コラム・記事一覧", "/columns")
    if idx == 72:
        import scheduled_articles
        if scheduled_articles.is_published("line-exchange-reality"):
            return ("相席ラウンジのLINE交換、なぜ翌日既読スルーされるのか？｜女性の記憶容量と連絡の境界線", "/guide/line-exchange-reality")
        return ("相席ラウンジ実践コラム・記事一覧", "/columns")
    return ("相席ラウンジ実践コラム・記事一覧", "/columns")


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
        if (col_path or "").startswith("/guide/"):
            slug = col_path.split("/guide/", 1)[1].strip("/")
            by_slug = scheduled_articles.get_article_by_slug(slug, allow_blocking=True)
            if by_slug:
                return by_slug.get("short_title") or by_slug.get("title") or "相席ラウンジ実践コラム"
        for item in scheduled_articles.get_published_articles(allow_blocking=True):
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
    return "相席ラウンジ実践コラム"


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
    When `standalone_no_reply=True`, omits the 'リプ欄へ' pointer for a pure 1-tweet reading experience.
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

    footer_tags_only = "\n\n詳しいコラム・現在の男女比はリプ欄へ\n#相席ラウンジ"

    candidate = f"{raw_tip}{footer_tags_only}"
    if x_poster.compute_twitter_weight(candidate) <= 276:
        return candidate

    short_tags = "\n\n詳細・現在人数はリプ欄へ\n#相席ラウンジ"
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
        f"関連コラム：{col_title}\n"
        f"{site_url}{col_path}?s=x{slot_hh}_tip"
    )


TIPS_RECENT_HISTORY_KEY = "tips_recent_history_v1"

HUMAN_OBSERVATION_SEEDS = (
    {"key": "first_drink_order", "lens": "乾杯の1杯目を頼む一瞬の空気感と、店員さんへの何気ない一言に出る余裕", "related_path": "/guide/first-10-minutes"},
    {"key": "silence_after_toast", "lens": "乾杯して10分後のふとした沈黙と、メニュー選びを一緒に楽しむ空気のほぐし方", "related_path": "/guide/first-10-minutes"},
    {"key": "buddy_chemistry", "lens": "男2人組で行くときの相方とのパス回し（相方をイジって下げる組と自然に立てる組の差）", "related_path": "/guide/solo-vs-pair"},
    {"key": "female_pair_eye_contact", "lens": "女子ペアが一瞬だけ目を合わせる合図や、友達側も退屈させない席の空気感", "related_path": "/guide/solo-vs-pair"},
    {"key": "sofa_70cm_grooming", "lens": "間接照明でもソファ席70cmだと意外と目に入る袖口・靴・夕方の肌感", "related_path": "/grooming"},
    {"key": "rain_hair_heels", "lens": "雨や湿気のある夜に女性側が一番気にしている前髪・足元と、地下直結店のありがたさ", "related_path": "/guide/rain-payday"},
    {"key": "smartphone_table_habit", "lens": "テーブルの上のスマホの置き方（画面上向きで通知を見る席と、自然にバッグへしまう席）", "related_path": "/guide/first-10-minutes"},
    {"key": "graceful_wrap_up_45m", "lens": "45分くらいで余韻を残して切り上げる組と、惰性で90分粘って疲れてしまう組の差", "related_path": "/guide/cost-break-even"},
    {"key": "second_venue_cafe_sweet", "lens": "2軒目に『もう1軒飲もう』ではなく『夜カフェや甘いもの30分だけ』と切り出す軽やかさ", "related_path": "/guide/second-venue"},
    {"key": "station_gate_5min_line", "lens": "改札で別れて5分以内に届く短い1行LINEと、帰宅後の長文メッセージの温度差", "related_path": "/guide/second-venue"},
    {"key": "declined_second_venue_grace", "lens": "2軒目を断られた瞬間の引き際の爽やかさで、実は翌週のご飯が決まっている話", "related_path": "/guide/second-venue"},
    {"key": "weekday_wed_thu_calm", "lens": "週末のガヤガヤ感とは違う、水曜・木曜20時台の仕事終わり層の落ち着いた会話テンポ", "related_path": "/guide/weekday-strategy"},
    {"key": "salon_return_early_evening", "lens": "美容院やネイル帰りの早い時間帯（18時半〜20時）にふらっと立ち寄る女子組の空気", "related_path": "/guide/beauty-area-stage"},
    {"key": "local_business_trip_night", "lens": "札幌・仙台・福岡・広島など出張先の夜に、現地の美味しい店を聞く会話の弾みやすさ", "related_path": "/guide/local-vs-urban"},
    {"key": "drink_pace_water_care", "lens": "お酒が強くない相手や2軒目組に、さらっとお水や温かいお茶を頼める気遣い", "related_path": "/guide"},
    {"key": "resume_interview_trap", "lens": "初対面で休日や住んでる駅を一問一答で聞いてしまう『面接モード』から抜ける雑談感", "related_path": "/guide/first-10-minutes"},
    {"key": "instagram_vs_group_line", "lens": "会計前の社交辞令グループLINEより、話が盛り上がった瞬間のインスタ交換が自然に残る理由", "related_path": "/guide/second-venue"},
    {"key": "payday_vs_quiet_week", "lens": "給料日直後の混雑した週末と、月の半ばの落ち着いた平日の居心地の違い", "related_path": "/guide/rain-payday"},
    {"key": "late_night_0030_second_wave", "lens": "終電が抜けた0時半すぎ、肩の力が抜けた2回転目の独特なゆるさと話しやすさ", "related_path": "/guide/club-area-midnight"},
    {"key": "seasonal_outerwear_scent", "lens": "強い香水よりも、季節に合った清潔なニットやシャツと控えめな柔軟剤くらいの距離感", "related_path": "/grooming"},
    {"key": "female_side_zero_wait_choice", "lens": "女の子2人組が『女性だらけの人気店』を避けて、待たずに座れる店を選ぶリアルな心理", "related_path": "/guide/male-lead-mechanics"},
    {"key": "table_change_politeness", "lens": "少し噛み合わない席でお互い気まずくならずに笑顔で交代を切り出せる大人の優しさ", "related_path": "/guide/table-change-protocol"},
    {"key": "pocket_bar_3min_walk", "lens": "外に出てから路上で検索を始めず、徒歩3分の静かなバーやカフェを1軒だけ知っている安心感", "related_path": "/guide/second-venue"},
    {"key": "friday_20pm_before_rush", "lens": "金土22時の大混雑に突っ込むより、19時台後半に一足早く乾杯している組のゆとり", "related_path": "/guide/crowd-rush-paradox"},
    {"key": "waveform_20pm_vs_midnight_flip", "lens": "20時台は女性先行だった店が深夜0時すぎに男性先行へひっくり返り、男女で『主役の時間帯』が入れ替わる現象", "related_path": "/guide/brand-waveform-asymmetry"},
    {"key": "same_city_brand_vibe_match", "lens": "同じ新宿や梅田でも、オリエンタルラウンジ・ag・JIS・ヤタコイ・ASOBIBARで集まる年齢感や1軒目/2軒目の空気がまるで違う面白さ", "related_path": "/guide/brand-waveform-asymmetry"},
    {"key": "heaven_for_men_wait_for_women", "lens": "男性にとって天国の『女性8割フロア』が、女子2人組にとっては相席がつかず手持無沙汰になりやすい非対称なリアル", "related_path": "/guide/brand-waveform-asymmetry"},
    {"key": "asobibar_standing_escape", "lens": "しっかり座る相席に少し疲れた2軒目や終電後に、ASOBIBARのような回遊スタイルで1杯だけ気楽に話す身軽さ", "related_path": "/guide/brand-waveform-asymmetry"},
    {"key": "yatakoi_ag_midnight_ladies", "lens": "早い時間は女子会ムードだったagやヤタコイが、深夜0時を回ると2軒目男性が増えて女子組が待ちゼロ即乾杯になる逆転リズム", "related_path": "/guide/brand-waveform-asymmetry"},
    {"key": "jis_early_vs_1am_monster", "lens": "19時台の静かな早割タイムと、深夜1時でも100人規模で熱気があるJISの二面性", "related_path": "/guide/brand-waveform-asymmetry"},
    {"key": "lounge_pair_vs_1on1_cafe", "lens": "友人ペアで今から乾杯する相席ラウンジのライブ感と、1人で週末に60分だけ話す1対1カフェデートの心地よい使い分け", "related_path": "/guide/lounge-vs-screened-date"},
    {"key": "spotlight_70cm_dress_code", "lens": "暗い店内でも真上の暖色スポット照明×70cm距離で意外と目に入る口元・靴先と、ブランドごとの浮かない私服バランス", "related_path": "/guide/dress-code-cleanliness"},
    {"key": "shibuya_vs_ebisu_one_stop", "lens": "渋谷が混雑ピークの金曜夜に、山手線で1駅3分の恵比寿へ移動しただけで急に会話のテンポが落ち着く温度差", "related_path": "/guide/shibuya-ebisu-comparison"},
    {"key": "ueno_nakamachi_adjacent_pair", "lens": "上野・仲町通りで徒歩1分に並ぶオリエンタルラウンジとagを見比べて、その夜の年齢層に合う方へすっと入る身軽さ", "related_path": "/guide/ueno-kinshicho-machida"},
    {"key": "omiya_yokohama_suburban_return", "lens": "都心の1次会から大宮や横浜へ戻ってきた22時過ぎに、地元組同士で終電を気にせず盛り上がる独特の安心感", "related_path": "/guide/yokohama-omiya-kanto"},
    {"key": "nagoya_sakae_nishiki_700m", "lens": "名古屋の錦三丁目と住吉エリアで、わずか数百メートル歩くだけで客層の雰囲気も2軒目の誘いやすさも変わる街の構造", "related_path": "/guide/nagoya-sakae-nishiki"},
    {"key": "tenjin_daimyo_compact_hop", "lens": "福岡・大名の徒歩2分圏内に3ブランドが揃っていて、屋台や夜カフェへの2軒目移動まで一連で完結する天神のコンパクトさ", "related_path": "/guide/fukuoka-tenjin-kyushu"},
    {"key": "hiroshima_shintenchi_185m", "lens": "広島・新天地公園まわりの半径185mに3店舗が集まり、1軒目の混み具合を見てから2分で切り替えられる回りやすさ", "related_path": "/guide/hiroshima-okayama-matsuyama"},
    {"key": "susukino_kokubuncho_winter_indoor", "lens": "札幌すすきのや仙台国分町の寒い夜に、1店舗へ熱気が集まるからこそ早い時間と0時過ぎでガラリと変わるフロアの表情", "related_path": "/guide/sapporo-sendai-north"},
)


def get_recent_tips_history(limit: int = 20, allow_blocking: bool = True) -> list:
    """Return recent zero-base Tips entries `[{'seed_key': ..., 'text': ..., 'ts': ...}]` from `job_state`."""
    try:
        import json
        import job_state
        raw = job_state.get_value(TIPS_RECENT_HISTORY_KEY, allow_blocking=allow_blocking)
        if raw:
            items = json.loads(raw)
            if isinstance(items, list):
                return items[:limit]
    except Exception:
        pass
    return []


def record_recent_tip_history(seed_key: str, text: str, now_jst: datetime.datetime = None):
    """Persist a posted zero-base Tip so none of the last 20 seed lenses or texts repeat."""
    txt = (text or "").strip()
    if not txt:
        return
    now_jst = now_jst or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    try:
        import json
        import job_state
        items = get_recent_tips_history(limit=24, allow_blocking=True)
        items = [it for it in items if isinstance(it, dict) and it.get("text") != txt]
        items.insert(0, {
            "seed_key": seed_key or "",
            "text": txt[:280],
            "ts": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
        })
        job_state.set_value(TIPS_RECENT_HISTORY_KEY, json.dumps(items[:22], ensure_ascii=False))
    except Exception as e:
        logger.warning(f"Failed to persist recent tip history: {e}")


def select_fresh_observation_seed(
    now_jst: datetime.datetime = None,
    col_path: str = "",
    slot_label: str = "18:00",
    recent_history: list = None,
) -> dict:
    """
    Pick a human-observation situation seed guaranteed NOT to overlap with the recent ~18-20 posted seeds.
    Gently prefers a seed aligned with `col_path` when unused recently, otherwise rotates deterministically.
    """
    now_jst = now_jst or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    hist = recent_history if recent_history is not None else get_recent_tips_history(limit=20, allow_blocking=False)
    recent_keys = {str(it.get("seed_key") or "") for it in hist[:18] if isinstance(it, dict)}

    fresh_pool = [s for s in HUMAN_OBSERVATION_SEEDS if s["key"] not in recent_keys]
    if not fresh_pool:
        recent_short = {str(it.get("seed_key") or "") for it in hist[:8] if isinstance(it, dict)}
        fresh_pool = [s for s in HUMAN_OBSERVATION_SEEDS if s["key"] not in recent_short] or list(HUMAN_OBSERVATION_SEEDS)

    slot_off = 0 if slot_label == "12:00" else 7
    base_idx = (now_jst.toordinal() * 3 + slot_off) % len(fresh_pool)
    if col_path:
        matching = [s for s in fresh_pool if s.get("related_path") == col_path]
        if matching:
            return matching[(now_jst.toordinal() + slot_off) % len(matching)]
    return fresh_pool[base_idx]


def _generate_ai_daily_human_tip(
    seed_tip_text: str,
    slot_label: str,
    standalone_no_reply: bool,
    col_title: str,
    now_jst: datetime.datetime,
    col_path: str = "",
) -> tuple:
    """
    Generate a zero-base, unpretentious human tabletop/relationship observation tweet
    from `HUMAN_OBSERVATION_SEEDS` + today's weekday/season/weather, strictly avoiding the last 20 Tips
    and cross-channel X endings.
    Returns `(tweet_text, used_seed_key)` or `("", "")` on fallback.
    """
    try:
        import sns_editorial
        if not sns_editorial.has_any_ai_key(allow_blocking=True):
            return "", ""
        weather_ctx = sns_editorial.fetch_live_weather_context(allow_blocking=True) or {}
        street_pulse = sns_editorial.fetch_tonight_street_pulse(now_jst=now_jst, allow_blocking=True) or {}
        cal_ctx = sns_editorial.build_calendar_context(now_jst)
        recent_tips = get_recent_tips_history(limit=20, allow_blocking=True)
        seed_obj = select_fresh_observation_seed(
            now_jst=now_jst,
            col_path=col_path,
            slot_label=slot_label,
            recent_history=recent_tips,
        )
        cross_x_texts = sns_editorial.get_recent_cross_channel_texts("x", limit=5, allow_blocking=False)
        recent_tip_texts = [str(it.get("text") or "")[:140] for it in recent_tips[:6] if isinstance(it, dict) and it.get("text")]
        banned_endings = sns_editorial.extract_sentence_endings(cross_x_texts + recent_tip_texts)

        # Even on thread days, half the time use a "silent tree" (zero 👇 / zero hashtags in main post)
        silent_tree = standalone_no_reply or ((now_jst.day + (0 if slot_label == "12:00" else 1)) % 2 == 0)
        ending_styles = (
            "体言止めや短い余韻（『〜の差。』『〜だったりする。』等）でさらっと終える",
            "気取らない素の丁寧語（『〜ですね。』『〜かもです。』等）で穏やかに終える",
            "友人との雑談のような自然な言い切り（『〜って思う。』『〜がちょうどいい。』等）で終える",
        )
        chosen_ending_hint = ending_styles[(now_jst.toordinal() + now_jst.hour) % len(ending_styles)]

        if standalone_no_reply:
            footer_rule = "今日は完全1ツイートの雑談回なので、『👇』や『リプ欄』への誘導もハッシュタグも一切書かず、余韻だけで終えること。"
        elif silent_tree:
            footer_rule = "今回は『サイレントツリー回』（関連URLは2ポスト目に静かに繋ぐ形式）なので、1ポスト目の本文末尾に『リプ欄へ👇』やハッシュタグは一切付けず、自然な文章の余韻だけで終えること。"
        else:
            footer_rule = "宣伝臭い誘導は書かず、自然な文章のあとに控えめに『#相席ラウンジ』を1個だけ添えるか、さらっとリプ欄に触れて終えること。"

        pulse_note = " ／ ".join(
            street_pulse.get(k) for k in ("national_vibe", "kanto", "kansai") if street_pulse.get(k)
        )
        ctx_info = {
            "slot": slot_label,
            "month_season": f"{now_jst.month}月",
            "weekday": f"{cal_ctx.get('weekday_ja', '')}曜",
            "calendar_vibe": f"{cal_ctx.get('tag', '')}",
            "weather": weather_ctx.get("adlib_hint") or weather_ctx.get("summary") or "",
            **({"tonight_street_vibe": pulse_note} if pulse_note else {}),
            "observation_lens": seed_obj["lens"],
        }
        cliche_list_str = "・".join(f"『{w}』" for w in sns_editorial.AI_CLICHE_WORDS)
        prompt = (
            "あなたは全国60店舗超の時間帯データと男女それぞれの本音をフラットな目線で観察している、気取らない大人の友人です。\n"
            "これから飲みに出る人も多い夕方に、Xでふと呟いた『実測の裏付けが滲む人間観察・街の法則ポスト（72〜118文字以内、X換算250ウェイト以下）』をゼロベースで1本書いてください。\n\n"
            f"{sns_editorial.HUMAN_INSIDER_VOICE_GUIDE}\n\n"
            "【執筆ルール】\n"
            "1. 下の `observation_lens`（今日の観察テーマ）を核に、ありきたりな一般恋愛マナーではなく『時間帯による店内の波の変わり方』『男女それぞれの本音・居心地の非対称』『同じ街のブランド（オリエンタルラウンジ・ag・JIS・ヤタコイ・ASOBIBAR）と気分の相性』などが自然に伝わる鋭く穏やかな1場面を切り取ること。\n"
            "2. 男女どちらの味方にも偏らず対等な観察者として書き、特定ブランドを下げる表現や『モニター見てたら』『バックヤードで』等の裏方アピール、『〜せよ』『鉄則』『絶対NG』のような説教口調は禁止。絵文字や『！』は0〜1個までに抑えること。\n"
            f"3. 今回の語尾トーン目安：{chosen_ending_hint}（ただし直近使った文末 {sns_editorial.json.dumps(banned_endings[:5], ensure_ascii=False)} の連続使用は避けること）。\n"
            f"4. 直近の過去Tips（{sns_editorial.json.dumps(recent_tip_texts[:5], ensure_ascii=False)}）と主張・例え・1行目の入り方が絶対に被らないようにすること。\n"
            f"5. URL（http...）、架空の店舗人数、角括弧【】見出し、単体の『オリエンタル』表記や『YATAKOI』『OLG』表記、および禁止語（{cliche_list_str}）は絶対に使わないこと。\n"
            f"6. {footer_rule}\n\n"
            f"【今日のコンテキスト】\n{sns_editorial.json.dumps(ctx_info, ensure_ascii=False)}\n\n"
            '出力はJSON形式のみ：{"main_text": "投稿本文"}'
        )
        parsed, _, _ = sns_editorial.call_editorial_ai_json(
            prompt=prompt,
            max_output_tokens=2000,
            thinking_budget=1280,
            total_deadline_sec=36.0,
            per_call_timeout_sec=24.0,
            now_jst=now_jst,
            prefer_fast=False,
        )
        if not parsed or not parsed.get("main_text"):
            return "", ""
        txt = sns_editorial._postprocess_ai_main_text(parsed["main_text"], channel="x")
        if "http://" in txt or "https://" in txt:
            return "", ""
        if standalone_no_reply or silent_tree:
            txt = txt.replace("👇", "").replace("リプ欄へ", "").replace("リプ欄", "").strip()
        pol_txt, _ = sns_editorial.polish_draft_to_human_voice(
            draft_text=txt,
            channel="x",
            research=None,
            slot_label=slot_label,
            now_jst=now_jst,
            silent_tree=(standalone_no_reply or silent_tree),
            recent_endings=banned_endings,
        )
        if pol_txt:
            txt = pol_txt
        wt = x_poster.compute_twitter_weight(txt)
        if 84 <= wt <= 272 and not any(w in txt for w in sns_editorial.AI_CLICHE_WORDS):
            return txt, seed_obj["key"]
    except Exception as e:
        logger.warning(f"Tip AI generation fallback: {e}")
    return "", ""


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

    if not force:
        in_cd, cd_reason = x_poster.check_x_auto_post_cooldown(min_interval_minutes=48, now_jst=now_jst)
        if in_cd:
            logger.info(f"Deferring X tip post ({slot_label}): {cd_reason}")
            return {"status": "skipped", "reason": cd_reason}
        over_b, b_reason = x_poster.check_x_daily_post_budget(now_jst=now_jst)
        if over_b:
            logger.info(f"Skipping X tip post ({slot_label}): {b_reason}")
            return {"status": "skipped", "reason": b_reason}

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
        reply_variants = (
            f"{col_title}\n{site_url}{col_path}?s=x{slot_hh}_tip",
            f"関連コラムはこちらに置いてあります\n{col_title}\n{site_url}{col_path}?s=x{slot_hh}_tip",
            f"関連コラム：{col_title}\n{site_url}{col_path}?s=x{slot_hh}_tip",
        )
        reply_text = reply_variants[(now_jst.day + now_jst.hour) % len(reply_variants)]

    raw_seed = TIPS_LIST[idx % len(TIPS_LIST)]
    ai_text, used_seed_key = _generate_ai_daily_human_tip(
        seed_tip_text=raw_seed,
        slot_label=slot_label,
        standalone_no_reply=standalone,
        col_title=col_title,
        now_jst=now_jst,
        col_path=col_path,
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
        record_recent_tip_history(used_seed_key or f"static_{idx}", text, now_jst=now_jst)
        try:
            import sns_editorial
            sns_editorial.record_recent_x_text(text, source=f"tip_{slot_label}", now_jst=now_jst)
        except Exception:
            pass
        if col_path:
            job_state.mark_done(f"x_col_used:{date_str}:{col_path}")

    result["tweet_text"] = text
    result["reply_text"] = reply_text or ""
    result["column_path"] = col_path
    result["seed_key"] = used_seed_key
    result["standalone_no_reply"] = standalone
    result["slot"] = slot_label
    return result
