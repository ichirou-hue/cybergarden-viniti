const NAMES = ["rag"];

const loaded = new Map();

export async function tracks() {
  if (loaded.size) return [...loaded.values()];
  for (const name of NAMES) {
    try {
      const module = await import(`./${name}.js`);
      if (module.default && module.default.track) loaded.set(name, module.default);
    } catch (error) {
      // Область ещё не подключена: дескриптора нет — штатное состояние.
      // Браузер при этом пишет один 404 в консоль; это ожидаемо до появления solar.js.
      if (error && error.code !== "ERR_MODULE_NOT_FOUND" && error.name !== "TypeError") {
        console.warn(`Не удалось загрузить дескриптор области «${name}»:`, error.message);
      }
    }
  }
  return [...loaded.values()];
}
