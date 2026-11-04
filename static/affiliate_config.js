/**
 * 相席ナビ アフィリエイト＆プロモーション統合管理マスター設定
 * 
 * 案件の追加・URL差し替え・カテゴリ変更は、このファイルを編集するだけで
 * サイト全体のカード・モーダル・バナーへ全自動で反映されます。
 */

const AFFILIATE_MASTER = {
    // 商業サイトでの有効化フラグ（社内モード・スタッフログイン時は自動的に無効）
    enabled: !(typeof window !== 'undefined' && window.APP_CONFIG && (window.APP_CONFIG.isStaff || window.APP_CONFIG.isStaffSession)) && (typeof IS_STAFF !== 'undefined' ? !IS_STAFF : true),

    // カテゴリ定義
    categories: [
        { id: 'all', name: 'すべて', icon: '' },
        { id: 'beauty', name: 'ヒゲ脱毛・スキンケア', icon: '', highlight: true },
        { id: 'night', name: '乾杯・翌朝ケア', icon: '', highlight: true },
        { id: 'dating', name: '恋活・近くの出会い', icon: '', highlight: true },
        { id: 'grooming', name: '服装・身だしなみ', icon: '' }
    ],

    // 案件マスター一覧
    items: [
        // ==========================================
        // 1. ヒゲ脱毛・スキンケア（清潔感ケア）
        // ==========================================
        {
            id: 'mens_hair_removal',
            category: 'beauty',
            featured: true,
            badge: '夕方の青ヒゲ対策に',
            tag: '医療ヒゲ脱毛',
            name: '仕事帰りでも口元が気にならない「メンズ医療ヒゲ脱毛」',
            shortTitle: 'メンズ医療ヒゲ脱毛',
            infeedTag: '青ヒゲ対策',
            infeedSub: '夕方以降も清潔感をキープ',
            infeedTitle: 'メンズ医療ヒゲ脱毛（初回プラン）',
            infeedCta: '詳細 ›',
            target: '夕方になるとヒゲが伸びてきたり、カミソリ負けで肌が荒れやすい方',
            desc: 'ラウンジの照明は少し暗めですが、ソファ席は距離が近いので口元の青ヒゲや肌荒れは意外と目に入ります。日頃から医療脱毛で整えておくと、仕事帰りにそのまま立ち寄っても口元を気にせず自然に会話を楽しめます。',
            points: [
                '夕方〜夜でもヒゲ剃り跡が気にならない医療レーザー脱毛',
                '月々1,000円台から始められる初回カウンセリングプランあり',
                '毎朝のヒゲ剃り時間が減り、カミソリ負けの肌荒れも防げる'
            ],
            cta: 'キャンペーン・公式サイトを見る ›',
            bannerImg: 'https://www27.a8.net/svt/bgt?aid=261020931365&wid=001&eno=01&mid=s00000014286015006000&mc=1',
            trackingImg: 'https://www17.a8.net/0.gif?a8mat=4BEL1F+61B8KY+328C+2HCB1D',
            url: '/link/mens_hair_removal'
        },
        {
            id: 'mens_skincare_clinic',
            category: 'beauty',
            featured: false,
            badge: 'テカリ・乾燥ケア',
            tag: 'メンズスキンケア',
            name: 'ベタつきや乾燥をサッと整える「ORBIS Mr.（オルビス ミスター）」',
            shortTitle: 'ORBIS Mr. スキンケア',
            infeedTag: '肌ケア',
            infeedSub: '顔のテカリ・乾燥対策に',
            infeedTitle: 'ORBIS Mr. メンズスキンケア',
            infeedCta: '詳細 ›',
            target: '夕方になるとおでこや鼻がテカりやすい方、何から肌ケアを始めればいいか迷っている方',
            desc: '1日仕事をしたあとの肌は、皮脂でテカったりエアコンで乾燥しがちです。オルビスのメンズライン「ORBIS Mr.」は洗顔と化粧水のシンプルなステップで肌が落ち着くので、普段あまりスキンケアをしてこなかった人にも続けやすい定番アイテムです。',
            points: [
                '顔のベタつき・テカリを抑えて清潔感のある肌をキープ',
                '洗顔＋化粧水のシンプルなケアで忙しい日でも続けやすい',
                'まずは気軽に試せる初回限定のトライアルセットあり'
            ],
            cta: '初回限定セット・公式を見る ›',
            bannerImg: 'https://www28.a8.net/svt/bgt?aid=261020931376&wid=001&eno=01&mid=s00000008657018014000&mc=1',
            trackingImg: 'https://www18.a8.net/0.gif?a8mat=4BEL1F+67V08I+1USQ+2Z90WX',
            url: '/link/mens_skincare_clinic'
        },

        // ==========================================
        // 1.5 乾杯・翌朝サポート（ナイトケアサプリ）
        // ==========================================
        {
            id: 'night_care_supplement',
            category: 'night',
            featured: true,
            badge: '飲み放題・2軒目の夜に',
            tag: '乾杯・ナイトケアサプリ',
            name: '翌朝もスマートに動きたい夜の「乾杯サポート・ナイトケアサプリ」',
            shortTitle: '乾杯サポート・ナイトケアサプリ',
            infeedTag: 'ナイトケア',
            infeedSub: '飲み放題・2軒目のお供に',
            infeedTitle: '乾杯前後のナイトケアサプリ',
            infeedCta: '詳細 ›',
            target: '平日夜の仕事帰りに飲み放題へ行く方、2軒目バーまで楽しんだ翌朝も軽やかに動きたい方',
            desc: '相席ラウンジは時間制の飲み放題が基本なので、会話が盛り上がって乾杯を重ねたり2軒目へ流れたりすると、ついペースが早くなりがちです。ポケットやカバンに忍ばせてサッと飲めるナイトケアサプリを習慣にしておくと、翌朝に仕事や予定がある夜でも余裕を持って楽しめます。',
            points: [
                '乾杯前後に水やドリンクと一緒にサッと取り入れやすい携帯設計',
                '飲み放題や2軒目バーまでしっかり楽しみたい夜のコンディション習慣に',
                'ジャケットやバッグに常備しておくと急な飲み会や相席の夜も安心'
            ],
            cta: '公式サイト・キャンペーンを見る ›',
            bannerImg: 'https://www20.a8.net/svt/bgt?aid=261021943470&wid=001&eno=01&mid=s00000016208001007000&mc=1',
            trackingImg: 'https://www19.a8.net/0.gif?a8mat=4BELTJ+7RTR3M+3H28+5ZU29',
            url: '/link/night_care_supplement'
        },

        // ==========================================
        // 2. 恋活・マッチングアプリ（タイプ別比較枠：Circuit X / Rentracks 対応）
        // ==========================================
        {
            id: 'dating_instant',
            category: 'dating',
            featured: true,
            badge: 'お店が混んでいる日に',
            tag: '即時合流・恋活アプリ',
            name: '【今日これから派】ラウンジが満席の夜にも使える「定番マッチングアプリ」',
            shortTitle: '近くで今日飲める恋活アプリ',
            infeedTag: '恋活アプリ',
            infeedSub: 'お店が混んでいる日・少人数の日の選択肢に',
            infeedTitle: '今から近くで飲める相手も探せる恋活アプリ',
            infeedCta: '詳細 ›',
            target: '週末にお店が満席で入れなかったときや、近くで今日これから飲める相手を探したい方',
            desc: '金曜や土曜の夜は、どこの店舗も男性の入店待ちが長くなることがあります。「今日これから飲みに行ける人」を探せる機能があるマッチングアプリをスマホに入れておくと、お店が混んでいる日でも予定を無駄にせず動けます。',
            points: [
                '近くで「今日これから飲みたい」相手を探せる機能つき',
                '事前に写真やプロフィールを見て気の合いそうな人と会える',
                '20代〜30代前半の登録者が多く、普段の恋活にも使いやすい'
            ],
            cta: '無料で登録して相手を探してみる ›',
            bannerImg: 'https://www24.a8.net/svt/bgt?aid=261019920124&wid=001&eno=01&mid=s00000027012001016000&mc=1',
            trackingImg: 'https://www15.a8.net/0.gif?a8mat=4BEK9C+21TRSI+5SFC+61RI9',
            url: '/link/dating_instant'
        },
        {
            id: 'dating_bachelor',
            category: 'dating',
            featured: false,
            badge: 'メッセージ不要・審査制',
            tag: 'ハイクラス・デート直結型',
            name: '【1対1カフェ・食事派】日程を入れるだけで週1回デートが決まる審査制アプリ',
            shortTitle: '審査制・週1カフェデートアプリ',
            infeedTag: '審査制デート',
            infeedSub: 'やり取り不要で1対1デートが決まる',
            infeedTitle: 'メッセージ不要・審査制のカフェデートアプリ',
            infeedCta: '詳細 ›',
            target: '複数人の相席より1対1で落ち着いて話したい方、アプリでのメッセージのやり取りが面倒な方',
            desc: '「相席ラウンジの賑やかな空気も楽しいけれど、たまには最初から1対1で落ち着いたカフェやバーで話したい」という男性に選ばれている審査制アプリ。希望の日時を入れるだけでAIが相手をセッティングしてくれるため、事前のメッセージ疲れがありません。',
            points: [
                '事前の「いいね」や長いメッセージ往復なしでデートが確定',
                '男女ともに2段階審査制のため、容姿・マナーの整った20代〜30代が中心',
                '実名や顔写真が一般公開されないため、身バレを気にせず利用できる'
            ],
            cta: '審査制デートアプリの公式詳細を見る ›',
            url: '/link/dating_bachelor'
        },

        // ==========================================
        // 3. ファッション・身だしなみ
        // ==========================================
        {
            id: 'spu_styleup',
            category: 'grooming',
            featured: true,
            badge: '私服選びに迷ったら',
            tag: 'メンズ服・コーデ定期便',
            name: '相席に着ていく服に迷わない「SPU スタイルアップ便」',
            shortTitle: 'SPU スタイルアップ便（きれいめ私服）',
            infeedTag: 'きれいめ私服',
            infeedSub: '服選びに迷わないコーデ定期便',
            infeedTitle: 'SPU スタイルアップ便（プロ監修コーデ）',
            infeedCta: '詳細 ›',
            target: '休日に着ていく私服を選ぶのが面倒な方、清潔感のある大人っぽい服を揃えたい方',
            desc: '「相席ラウンジに何を着ていけば浮かないかな…」と迷う方には、スタイリストが選んだきれいめコーデが毎月届く「SPU スタイルアップ便」が便利です。派手すぎず落ち着いた服が一式揃うので、そのまま着ていくだけで自然な清潔感が出せます。',
            points: [
                'プロのスタイリストが選んだシンプルで清潔感のある大人コーデ',
                '自分で買いに行く手間がなく、届いた服をそのまま着られる',
                '不要な月はスキップもできるので自分のペースで使える'
            ],
            cta: '今月のコーデ内容を見てみる ›',
            bannerImg: 'https://h.accesstrade.net/sp/rr?rk=0100oo8500p144',
            url: '/link/spu_styleup'
        }
    ],

    // ==========================================
    // 💄 女性目線モード（audience === 'women'）専用カテゴリ＆案件マスター
    // ==========================================
    womenCategories: [
        { id: 'all', name: 'すべて', icon: '' },
        { id: 'w_dating', name: '審査制・1対1カフェデート', icon: '', highlight: true },
        { id: 'w_clinic', name: '美肌クリニック・医療脱毛', icon: '', highlight: true },
        { id: 'w_cosme', name: '夜ケア・翌朝サポート', icon: '' }
    ],

    womenItems: [
        {
            id: 'women_dating_bachelor',
            category: 'w_dating',
            featured: true,
            badge: '女性は完全無料・顔出し不要',
            tag: '審査制ハイクラスデート',
            name: '【1対1で落ち着いて話したい日に】日程を入れるだけで週1回デートが決まる審査制アプリ',
            shortTitle: '女性完全無料・審査制カフェデート',
            infeedTag: '女性完全無料',
            infeedSub: '顔出し・メッセージ往復なし',
            infeedTitle: '審査制1対1カフェデート（日程登録のみ）',
            infeedCta: '女性無料の詳細 ›',
            target: '複数人の賑やかな相席より1対1のカフェやバーで落ち着いて話したい方・身バレせずにハイスペック層と出会いたい方',
            desc: '相席ラウンジで「今日はちょっと席のテンションが合わなかったかも…」という日の選択肢として女性人気が高い2段階審査制アプリ。顔写真の一般公開がなく、いいね送りや面倒なメッセージ往復もゼロでAIが週末のカフェデートをセッティングしてくれます。',
            points: [
                '女性は登録からデートまで完全無料（男性側も審査通過者のみ）',
                '顔写真・実名がアプリ上に公開されないため友達や職場に身バレしない',
                'デート後のレポート評価制で、マナーの良い20代後半〜30代男性が中心'
            ],
            cta: '【女性無料】審査制デートの公式詳細を見る ›',
            url: '/link/women_dating_bachelor'
        },
        {
            id: 'women_beauty_clinic',
            category: 'w_clinic',
            featured: true,
            badge: '間接照明でも映える肌へ',
            tag: '美容医療・医療脱毛',
            name: '仕事帰り・すっぴん風メイクでも自信が持てる「美容皮膚科・医療脱毛初回プラン」',
            shortTitle: '美肌クリニック・医療脱毛 初回体験',
            target: 'ラウンジの近距離ソファ席でも毛穴・くすみを気にせず過ごしたい方、自己処理の手間を減らしたい方',
            desc: 'ラウンジの暖色ライトは雰囲気が出る反面、向かい合わせ70cmの距離ではうぶ毛や肌のキメが意外と映えます。大手美容クリニックの初回カウンセリングや美肌・医療脱毛トライアルは1回あたり手頃な価格で試せるため、自分磨きのメンテナンスに選ばれています。',
            points: [
                '全国主要駅チカで仕事帰りや買い物ついでに通いやすい大手クリニック',
                '無理な勧誘なしで相談できる無料カウンセリング＆初回限定プラン',
                'メイクノリが変わる美肌ケア・医療レーザー脱毛を気軽にスタート可能'
            ],
            cta: '初回キャンペーン・対応院を見る ›',
            url: '/link/women_beauty_clinic'
        },
        {
            id: 'women_cosme_salon',
            category: 'w_cosme',
            featured: true,
            badge: '夜遊び帰りの髪・肌リセット',
            tag: 'サロン級ヘア＆ナイトケア',
            name: '遅くなった夜もサッと整う「サロン品質ヘア＆ご褒美スキンケア」',
            shortTitle: 'サロン品質ヘア＆スキンケア',
            target: 'お酒を飲んだ翌日やエアコンの効いた店内帰りの髪・肌の乾燥を翌朝に残したくない方',
            desc: '空調が効いたラウンジでお酒やスイーツを楽しんだ夜は、思った以上に髪や肌の水分が奪われがち。帰宅後すぐにケアできるサロン品質のアイテムを取り入れておくと、翌朝のまとまりと化粧ノリがぐっと安定します。',
            points: [
                '夜遅く帰った日でもシンプルなステップでサロン級のうるおいケア',
                'ラウンジの間接照明でも映えるツヤ髪・うるおい肌をキープ',
                '自宅で手軽に始められる公式オンライン限定ラインナップ'
            ],
            cta: '公式ショップで詳細を見る ›',
            bannerImg: 'https://www29.a8.net/svt/bgt?aid=261029019395&wid=001&eno=01&mid=s00000025528001003000&mc=1',
            trackingImg: 'https://www17.a8.net/0.gif?a8mat=4BERA3+6J68QA+5GZ4+5YZ75',
            url: '/link/women_cosme_salon'
        },
        {
            id: 'women_night_supplement',
            category: 'w_cosme',
            featured: true,
            badge: '乾杯・カクテルを楽しむ夜に',
            tag: 'ナイトケア・翌朝サポート',
            name: '飲み放題やワインを楽しむ夜の「乾杯サポート・ナイトケアサプリ」',
            shortTitle: '乾杯サポート・ナイトケアサプリ',
            target: 'カクテルやワインを楽しんだ翌朝も、朝から予定通り軽やかに動きたい方',
            desc: '女性無料の飲み放題でカクテルやスパークリングを楽しんでいると、気づけばいつもより杯数が進んでいることも。ミニバッグやポーチに入れておけるナイトケアサプリを乾杯前後のルーティンにしておくと、翌日の仕事や予定にも響きにくく安心です。',
            points: [
                'ミニバッグやポーチにすっきり収まり、乾杯前後にサッと飲める',
                'カクテル・ワイン・2軒目バーを楽しむ夜のスマートなコンディション習慣',
                '友達の分もシェアしやすい大人のナイトケアアイテムとして人気'
            ],
            cta: '公式サイトで詳細を見る ›',
            bannerImg: 'https://www20.a8.net/svt/bgt?aid=261021943470&wid=001&eno=01&mid=s00000016208001007000&mc=1',
            trackingImg: 'https://www19.a8.net/0.gif?a8mat=4BELTJ+7RTR3M+3H28+5ZU29',
            url: '/link/women_night_supplement'
        }
    ],

    // 4位直下の文脈連動インフィード導線（時間帯・上位店の混雑状況・男女モードに応じて自動出し分け）
    infeedSlots: [
        {
            afterRank: 4,
            slotKey: 'rank4_context',
            contextual: true,
            itemId: 'dating_bachelor',
            crowdedItemId: 'dating_instant',
            daytimeItemId: 'mens_hair_removal',
            womenItemId: 'women_dating_bachelor'
        }
    ]
};
