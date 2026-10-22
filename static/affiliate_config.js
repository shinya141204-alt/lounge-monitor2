/**
 * 相席ナビ アフィリエイト＆プロモーション統合管理マスター設定
 * 
 * 案件の追加・URL差し替え・カテゴリ変更は、このファイルを編集するだけで
 * サイト全体のカード・モーダル・バナーへ全自動で反映されます。
 */

const AFFILIATE_MASTER = {
    // 商業サイトでの有効化フラグ（社内モード時は自動的に無効）
    enabled: typeof IS_STAFF !== 'undefined' ? !IS_STAFF : true,

    // カテゴリ定義
    categories: [
        { id: 'all', name: 'すべて', icon: '✨' },
        { id: 'beauty', name: 'ヒゲ脱毛・スキンケア', icon: '🪒', highlight: true },
        { id: 'dating', name: '恋活・近くの出会い', icon: '💘', highlight: true },
        { id: 'grooming', name: '服装・身だしなみ', icon: '👔' }
    ],

    // 案件マスター一覧
    items: [
        // ==========================================
        // 🪒 1. ヒゲ脱毛・スキンケア（清潔感ケア）
        // ==========================================
        {
            id: 'mens_hair_removal',
            category: 'beauty',
            featured: true,
            badge: '🪒 夕方の青ヒゲ対策に',
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
            url: 'https://px.a8.net/svt/ejp?a8mat=4BEL1F+61B8KY+328C+2HCB1D'
        },
        {
            id: 'mens_skincare_clinic',
            category: 'beauty',
            featured: false,
            badge: '💎 テカリ・乾燥ケア',
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
            url: 'https://px.a8.net/svt/ejp?a8mat=4BEL1F+67V08I+1USQ+2Z90WX'
        },

        // ==========================================
        // 💘 2. 恋活・マッチングアプリ
        // ==========================================
        {
            id: 'dating_instant',
            category: 'dating',
            featured: true,
            badge: '📱 お店が混んでいる日に',
            tag: '恋活・マッチング',
            name: 'ラウンジが満席のときにも使える「定番マッチングアプリ」',
            shortTitle: '近くで会える恋活アプリ',
            infeedTag: '恋活アプリ',
            infeedSub: 'お店が混んでいる日の選択肢に',
            infeedTitle: '今から飲める相手も探せる恋活アプリ',
            infeedCta: '見る ›',
            target: '週末にお店が満席で入れなかったときや、1対1で落ち着いて飲める相手を探したい方',
            desc: '金曜や土曜の夜は、どこの店舗も男性の入店待ちが長くなることがあります。「今日これから飲みに行ける人」を探せる機能があるマッチングアプリをスマホに入れておくと、お店が混んでいる日でも予定を無駄にせず動けます。',
            points: [
                '近くで「今日これから飲みたい」相手を探せる機能つき',
                '事前に写真やプロフィールを見て気の合いそうな人と会える',
                '20代〜30代前半の登録者が多く、普段の恋活にも使いやすい'
            ],
            cta: '無料で登録して相手を探してみる ›',
            bannerImg: 'https://www24.a8.net/svt/bgt?aid=261019920124&wid=001&eno=01&mid=s00000027012001016000&mc=1',
            trackingImg: 'https://www15.a8.net/0.gif?a8mat=4BEK9C+21TRSI+5SFC+61RI9',
            url: 'https://px.a8.net/svt/ejp?a8mat=4BEK9C+21TRSI+5SFC+61RI9'
        },

        // ==========================================
        // 👔 3. ファッション・身だしなみ
        // ==========================================
        {
            id: 'spu_styleup',
            category: 'grooming',
            featured: true,
            badge: '👔 私服選びに迷ったら',
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
            url: 'https://h.accesstrade.net/sp/cc?rk=0100oo8500p144'
        }
    ],

    // ランキング一覧に自然に挟み込むインフィードカード配置設定
    infeedSlots: [
        {
            afterRank: 5,
            itemId: 'dating_instant',
            badgeText: 'PR',
            highlight: false
        },
        {
            afterRank: 10,
            itemId: 'mens_hair_removal',
            badgeText: 'PR',
            highlight: true
        },
        {
            afterRank: 18,
            itemId: 'mens_skincare_clinic',
            badgeText: 'PR',
            highlight: false
        },
        {
            afterRank: 25,
            itemId: 'spu_styleup',
            badgeText: 'PR',
            highlight: false
        }
    ]
};
