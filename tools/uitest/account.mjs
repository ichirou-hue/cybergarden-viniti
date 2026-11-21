// Общая фикстура аккаунта для dev-проверок: экран первого входа обязателен,
// поэтому каталог и паспорт открываются только с записью в localStorage.
// Ключ и поля совпадают с auth.js (STORE_KEY, normalize).

export const TEST_ACCOUNT = {
  name: "Тестовый Пользователь",
  email: "test@viniti.ru",
  org: "ВИНИТИ РАН",
  role: "Исследователь",
  createdAt: 1763769600000,
  stats: {docs: 0, actions: 0},
  activity: [],
};

export function seedWindow(target, account = TEST_ACCOUNT) {
  target.localStorage.setItem("viniti.account.v1", JSON.stringify(account));
}

export async function seedContext(context, account = TEST_ACCOUNT) {
  await context.addInitScript(value => {
    try {
      localStorage.setItem("viniti.account.v1", JSON.stringify(value));
    } catch (error) {
      // Хранилище недоступно: проверка сама сообщит, что экран входа не ушёл.
    }
  }, account);
}
