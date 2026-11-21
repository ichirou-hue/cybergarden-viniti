// Общая фикстура аккаунта для dev-проверок. Вход теперь проверяет сервер,
// поэтому контексту нужны cookie сессии (мок-сервер: mock_session), а в
// localStorage остаётся только профиль пользователя под его id.
// Ключ совпадает с auth.js (PROFILE_PREFIX).

export const TEST_ACCOUNT = {
  name: "Тестовый Пользователь",
  email: "test@viniti.ru",
  org: "ВИНИТИ РАН",
  role: "Исследователь",
  createdAt: 1763769600000,
  stats: {docs: 0, actions: 0},
  activity: [],
};

export const TEST_SESSION_COOKIE = "mock_session";
const MOCK_URL = process.env.MOCK_URL || "http://127.0.0.1:8799";

function profileKey(account) {
  return `viniti.profile.v1:${account.email}`;
}

export function seedWindow(target, account = TEST_ACCOUNT) {
  target.localStorage.setItem(profileKey(account), JSON.stringify(account));
}

export async function seedContext(context, account = TEST_ACCOUNT) {
  const entry = {key: profileKey(account), value: JSON.stringify(account)};
  await context.addInitScript(item => {
    try {
      localStorage.setItem(item.key, item.value);
    } catch (error) {
      // Хранилище недоступно: проверка сама сообщит, что экран входа не ушёл.
    }
  }, entry);
  await context.addCookies([{
    name: TEST_SESSION_COOKIE,
    value: encodeURIComponent(account.email),
    url: MOCK_URL,
  }]);
}

// JSDOM-проверки ходят в мок через Node fetch без cookie jar: подменяем
// только /api/auth/me, остальное отдаёт настоящий мок.
export function sessionResponse(username = TEST_ACCOUNT.email) {
  const body = JSON.stringify({
    user: {id: username, username, created_at: 1763769600},
    csrf_token: "mock-csrf-token",
    expires_at: 4102444800,
  });
  return {
    ok: true,
    status: 200,
    headers: new Headers({"Content-Type": "application/json; charset=utf-8"}),
    json: async () => JSON.parse(body),
    text: async () => body,
  };
}
