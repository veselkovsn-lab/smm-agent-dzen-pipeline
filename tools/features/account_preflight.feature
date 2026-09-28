Feature: Доступ к рабочим каналам без публикаций
  Scenario: ACC01 Ключи есть, но локальных входов ещё нет
    Given настройки каналов без сохранённых сессий
    When выполняется read-only preflight двух аккаунтов
    Then оба аккаунта требуют локального входа без внешних публикаций

  Scenario: ACC02 OAuth относится к другому YouTube-каналу
    Given сохранённая OAuth-сессия другого канала
    When выполняется read-only preflight YouTube
    Then YouTube заблокирован по несовпадению канала без загрузки

  Scenario: ACC03 Пользователь Telegram не может публиковать
    Given сохранённая Telegram-сессия без права публикации
    When выполняется read-only preflight Telegram
    Then Telegram заблокирован по правам без сообщения

  Scenario: ACC04 Нужные каналы и права подтверждены
    Given сохранённые сессии целевых каналов
    When выполняется read-only preflight двух аккаунтов
    Then оба аккаунта готовы без внешних публикаций
