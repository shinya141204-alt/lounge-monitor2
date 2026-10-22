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
        { id: 'beauty', name: 'メンズ脱毛・美肌', icon: '🪒', highlight: true },
        { id: 'dating', name: '恋活・即日出会い', icon: '💘', highlight: true },
        { id: 'grooming', name: 'ファッション・身だしなみ', icon: '👔' }
    ],

    // 案件マスター一覧
    items: [
        // ==========================================
        // 🪒 1. メンズ脱毛・美肌・清潔感（最注力ジャンル）
        // ==========================================
        {
            id: 'mens_hair_removal',
            category: 'beauty',
            featured: true,
            badge: '🔥 至近距離の必須マナー',
            tag: '青ヒゲ・医療脱毛',
            name: '相席ラウンジで圧倒的にモテる！メンズ医療ヒゲ脱毛プログラム',
            shortTitle: 'メンズ医療ヒゲ脱毛特集',
            infeedTag: '青ヒゲ対策',
            infeedSub: '至近距離の必須マナー',
            infeedTitle: 'メンズ医療ヒゲ脱毛プログラム',
            infeedCta: '公式 ›',
            target: '至近距離の会話で青ヒゲ・肌荒れが気になり、第一印象を劇的に良くしたい男性',
            desc: '照明が落ち着いた相席ラウンジでは、女性は男性の「口元の清潔感」を真っ先に見ています。夕方〜深夜でも青ヒゲが目立たない肌を整えることで、LINE交換率と2軒目への誘いやすさが段違いに跳ね上がります。',
            points: [
                '夜の店内でも清潔感をキープできる医療レーザー脱毛',
                '月々1,000円台〜手軽に始められる初回カウンセリングプラン',
                '相席した20代女性からの好感度・清潔感評価が劇的向上'
            ],
            cta: '限定キャンペーン・公式を見る ›',
            bannerImg: 'https://www27.a8.net/svt/bgt?aid=261020931365&wid=001&eno=01&mid=s00000014286015006000&mc=1',
            trackingImg: 'https://www17.a8.net/0.gif?a8mat=4BEL1F+61B8KY+328C+2HCB1D',
            url: 'https://px.a8.net/svt/ejp?a8mat=4BEL1F+61B8KY+328C+2HCB1D'
        },
        {
            id: 'mens_skincare_clinic',
            category: 'beauty',
            featured: false,
            badge: '💎 好感度UP',
            tag: '美肌・毛穴スキンケア',
            name: '女性ウケ抜群の第一印象を作る！ORBIS Mr.（オルビス ミスター）スキンケア',
            shortTitle: 'ORBIS Mr. スキンケア特集',
            infeedTag: '美肌ケア',
            infeedSub: 'テカリ・毛穴を徹底対策',
            infeedTitle: 'ORBIS Mr. メンズスキンケア',
            infeedCta: '公式 ›',
            target: 'テカリ・乾燥・毛穴を防ぎ、清潔感ある大人のモテ肌を手に入れたい男性',
            desc: '相席ラウンジのダウンライト下でも上品に見える「清潔な素肌」を作る人気メンズスキンケア「ORBIS Mr.（オルビス ミスター）」。洗顔・化粧水の簡単ケアで、初対面の第一印象を劇的に引き上げます。',
            points: [
                'ベタつき・テカリを抑えて爽やかな清潔感を長時間キープ',
                '20代〜30代女性に選ばれる好印象メンズスキンケア',
                '手軽に試せる初回限定スターターセットあり'
            ],
            cta: '初回限定セット・公式を見る ›',
            bannerImg: 'https://www28.a8.net/svt/bgt?aid=261020931376&wid=001&eno=01&mid=s00000008657018014000&mc=1',
            trackingImg: 'https://www18.a8.net/0.gif?a8mat=4BEL1F+67V08I+1USQ+2Z90WX',
            url: 'https://px.a8.net/svt/ejp?a8mat=4BEL1F+67V08I+1USQ+2Z90WX'
        },

        // ==========================================
        // 💘 2. 恋活・マッチング・即日出会い系
        // ==========================================
        {
            id: 'dating_instant',
            category: 'dating',
            featured: true,
            badge: '👑 待ち時間ゼロ',
            tag: '即日恋活・マッチング',
            name: '満席の待ち時間を回避！今夜すぐ近くで出会える人気マッチングアプリ',
            shortTitle: '即日出会える恋活アプリ特集',
            infeedTag: '即日恋活',
            infeedSub: '満席時の待ち時間ゼロ',
            infeedTitle: '即日出会える人気マッチングアプリ',
            infeedCta: '探す ›',
            target: '相席ラウンジの入店待ちが長い時や、今夜中に確実に飲める相手を見つけたい男性',
            desc: '「金曜夜に相席ラウンジに行ったら満席で1時間待ち…」そんな時に最も頼れるのが「今日これから会える」機能付きマッチングアプリ。待ち時間にアポを組むことで週末の空振りをゼロにできます。',
            points: [
                'GPSで現在地周辺の「いま飲みたい相手」を検索可能',
                '相席ラウンジ前の二刀流で週末の出会い成功率が倍増',
                '女性会員数が多く20代〜30代前半の利用者が急増中'
            ],
            cta: '無料登録・今夜の相手を探す ›',
            bannerImg: 'https://www24.a8.net/svt/bgt?aid=261019920124&wid=001&eno=01&mid=s00000027012001016000&mc=1',
            trackingImg: 'https://www15.a8.net/0.gif?a8mat=4BEK9C+21TRSI+5SFC+61RI9',
            url: 'https://px.a8.net/svt/ejp?a8mat=4BEK9C+21TRSI+5SFC+61RI9'
        },

        // ==========================================
        // 👔 4. 香水・身だしなみ・ファッション系
        // ==========================================
        {
            id: 'spu_styleup',
            category: 'grooming',
            featured: true,
            badge: '👔 プロ監修モテ私服',
            tag: 'メンズ服・コーデ定期便',
            name: '相席ラウンジで外さない！スタイリスト厳選コーデが毎月届く【SPU スタイルアップ便】',
            shortTitle: 'SPU スタイルアップ便特集',
            infeedTag: 'モテ私服',
            infeedSub: 'スタイリスト厳選コーデ',
            infeedTitle: 'SPU スタイルアップ便（モテ服）',
            infeedCta: '公式 ›',
            target: '相席ラウンジに着ていく私服に迷う方、女性ウケする大人の清潔感コーデを一式揃えたい男性',
            desc: '「相席ラウンジに何を着ていけばいいかわからない…」という悩みをプロが一発解決！スタイリストが女性目線で好感度の高い「きれいめモテコーデ」を毎月厳選して自宅に届ける人気サブスク。そのまま着るだけで洗練された大人の印象を作れます。',
            points: [
                'プロスタイリスト監修！女性ウケ抜群のキレイめ大人カジュアル',
                'コーディネートに悩む時間をゼロにして週末の相席ラウンジへ直行',
                '不要な月はスキップ可能・いつでも手軽にお試しOK'
            ],
            cta: 'スタイリスト厳選コーデを見る ›',
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
