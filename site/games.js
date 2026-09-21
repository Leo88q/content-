// ============================================================
// СЛОТ ИГР — заполнить реальными играми.
// url — deep link с UTM (атрибуция трафика из пылесоса в игру).
// Все клики по слотам логируются в window.__SLOT_CLICKS__
// и шлют событие ?evt=click_slot&id=... (заглушка пикселя).
// ============================================================
window.GAMES = [
  {
    id: "game1",
    name: "Ваша игра #1",
    tagline: "Заполните: жанр + одно предложение хука",
    url: "https://example.com/game1?utm_source=talkchart&utm_medium=related_card&utm_campaign=vacuum_pilot",
    cta: "Играть",
    accent: "#7cf03d"
  },
  {
    id: "game2",
    name: "Ваша игра #2",
    tagline: "Заполните: жанр + одно предложение хука",
    url: "https://example.com/game2?utm_source=talkchart&utm_medium=related_card&utm_campaign=vacuum_pilot",
    cta: "Играть",
    accent: "#3dd9f0"
  },
  {
    id: "game3",
    name: "Ваша игра #3",
    tagline: "Заполните: жанр + одно предложение хука",
    url: "https://example.com/game3?utm_source=talkchart&utm_medium=related_card&utm_campaign=vacuum_pilot",
    cta: "Играть",
    accent: "#f0b13d"
  },
  {
    id: "game4",
    name: "Ваша игра #4",
    tagline: "Заполните: жанр + одно предложение хука",
    url: "https://example.com/game4?utm_source=talkchart&utm_medium=interstitial&utm_campaign=vacuum_pilot",
    cta: "Играть",
    accent: "#f03d9c"
  }
];

// Как часто показывать playable-интерстишал: после каждого N-го
// переключения пула. Правило прокладки: <=1 слот на 5 действий.
window.INTERSTITIAL_EVERY = 5;

// ============================================================
// TIPLINK ONBOARDING (без сид-фраз: вход через Google)
// Позволяет онбордить новых игроков без установки кошельков.
// Игрок переходит по ссылке, авторизуется через Google (Web3Auth/TipLink)
// и сразу получает стартовый стек кредитов в игры студии.
// Замените claimUrl на реальный URL кампании в TipLink Pro / TipLink API.
// ============================================================
window.TIPLINK_CONFIG = {
  enabled: true,
  title: "🎁 Стартовый стек (без сид-фраз)",
  description: "Вход через Google за 2 секунды. Забери приветственные кредиты в игры студии:",
  cta: "Забрать TipLink через Google 🎁",
  claimUrl: "https://tiplink.io/campaign/talkchart-starter?utm_source=talkchart&utm_medium=tiplink_pass",
  streakBonusCredits: 500
};

