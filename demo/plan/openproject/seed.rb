# Демо-проект в OpenProject: структура, которую агент потом читает как дорожную карту.
# Поля нашего процесса (стадия, критичность для ввода, дата входа, возвратов) заводятся
# пользовательскими полями — у заказчика они будут называться иначе, поэтому их идентификаторы
# агент получает из рецепта, а не из кода.

def cf!(name, format, opts = {})
  cf = WorkPackageCustomField.find_by(name: name)
  return cf if cf
  cf = WorkPackageCustomField.new(name: name, field_format: format, is_for_all: true,
                                  is_required: false)
  cf.possible_values = opts[:values] if opts[:values]
  cf.save!
  cf
end

stage_values = ["Поставка", "Проверка АПСЕК", "Развёртывание на среде",
                "Функциональное тестирование", "Сквозное тестирование (E2E)",
                "ПСИ (предварительные испытания)", "ЗНИ (замечания и несоответствия)",
                "Вывод в пром", "Тиражирование"]
cf_stage = cf!("Стадия процесса", "list", values: stage_values)
cf_crit  = cf!("Критично для ввода", "bool")
cf_entry = cf!("Дата входа на стадию", "date")
cf_back  = cf!("Возвратов на предыдущую стадию", "int")

project = Project.find_by(identifier: "prj-2451")
unless project
  project = Project.create!(name: "PRJ-2451 Модернизация тарификации",
                            identifier: "prj-2451",
                            description: "Демо-проект ABOP: график поставки функционала по " \
                                         "регламенту производственного процесса.")
end
[cf_stage, cf_crit, cf_entry, cf_back].each do |cf|
  project.work_package_custom_fields << cf unless project.work_package_custom_fields.include?(cf)
end
# Без модуля задач права view_work_packages нет даже у администратора: в OpenProject права
# считаются от включённых модулей, а Project.create! не включает ни одного. API отвечал 403.
project.enabled_module_names = (project.enabled_module_names | %w[work_package_tracking]).uniq
# Тип задачи берём из того, что есть в установке: при русской локали он называется иначе,
# и поиск по английскому имени возвращал nil.
default_type = Type.where(is_default: true).first || Type.where(is_milestone: false).first || Type.first
project.types << default_type if default_type && !project.types.include?(default_type)
project.save!
# Поле приезжает в API только если включено И в проекте, И в форме типа задачи. Иначе ответ
# приходит без customFieldN, и адаптер получает пустую стадию — молча, без ошибки.
project.types.each do |t|
  t.custom_field_ids = (t.custom_field_ids | [cf_stage.id, cf_crit.id, cf_entry.id, cf_back.id]).uniq
  t.save!
end

# Автор — ЖИВОЙ администратор: у встроенного системного пользователя тоже admin = true,
# но членства в проектах у него нет, и API потом отвечает 403 MissingPermission.
author = User.where(admin: true).reject(&:builtin?).first || User.find_by(login: "admin")
raise "не найден живой администратор" unless author
type   = project.types.first || default_type
status_new = Status.find_by(is_default: true) || Status.first
# Приоритет обязателен на уровне БД: запись в журнал падала с NotNullViolation.
priority_default = IssuePriority.active.find_by(is_default: true) || IssuePriority.active.first ||
                   IssuePriority.first

ITEMS = [
  {n: "1", subj: "Модуль расчёта тарифов",            stage: "Вывод в пром",
   start: "2026-08-03", due: "2026-10-16", crit: true,  entry: "2026-10-12", back: 0, done: 90},
  {n: "2", subj: "Интеграция с биллингом",            stage: "Сквозное тестирование (E2E)",
   start: "2026-08-17", due: "2026-10-09", crit: true,  entry: "2026-09-28", back: 0, done: 70},
  {n: "3", subj: "Отчётность для регулятора",         stage: "ПСИ (предварительные испытания)",
   start: "2026-09-07", due: "2026-10-23", crit: true,  entry: "2026-10-05", back: 1, done: 55},
  {n: "4", subj: "Личный кабинет абонента",           stage: "Функциональное тестирование",
   start: "2026-09-01", due: "2026-10-02", crit: false, entry: "2026-09-21", back: 0, done: 60},
  {n: "5", subj: "Миграция справочников",             stage: "Развёртывание на среде",
   start: "2026-09-14", due: "2026-10-15", crit: false, entry: "2026-10-13", back: 0, done: 40},
  {n: "6", subj: "Региональные площадки: тиражирование", stage: "Тиражирование",
   start: "2026-10-26", due: "2026-11-20", crit: true,  entry: nil,          back: 0, done: 0},
  {n: "7", subj: "Доработка тарифов по замечаниям АПСЕК", stage: "Проверка АПСЕК",
   start: "2026-09-28", due: "2026-10-07", crit: true,  entry: "2026-10-02", back: 0, done: 30},
]
# «Ждёт» в терминах OpenProject — отношение follows: 2 follows 1 означает «2 после 1».
FOLLOWS = {"2" => ["1"], "3" => ["2"], "5" => ["4"], "6" => ["1", "7"]}

made = {}
ITEMS.each do |it|
  wp = WorkPackage.find_by(subject: it[:subj], project_id: project.id)
  wp ||= WorkPackage.new(project: project, type: type, author: author, status: status_new)
  wp.priority ||= priority_default
  wp.subject = it[:subj]
  wp.start_date = Date.parse(it[:start])
  wp.due_date = Date.parse(it[:due])
  wp.done_ratio = it[:done]
  # Поле-список хранит ИДЕНТИФИКАТОР варианта, а не его текст: от текста в API приезжает
  # «<значение> не найдено», и стадия до движка не доходит.
  вариант = cf_stage.custom_options.find { |o| o.value == it[:stage] }
  raise "нет варианта стадии «#{it[:stage]}»" unless вариант
  wp.custom_field_values = {
    cf_stage.id => вариант.id.to_s,
    cf_crit.id => (it[:crit] ? "t" : "f"),
    cf_entry.id => it[:entry],
    cf_back.id => it[:back].to_s,
  }
  wp.save!(validate: false)
  made[it[:n]] = wp
end

FOLLOWS.each do |n, parents|
  parents.each do |p|
    from, to = made[n], made[p]
    next unless from && to
    next if Relation.where(from_id: from.id, to_id: to.id).exists?
    Relation.create!(from: from, to: to, relation_type: Relation::TYPE_FOLLOWS)
  end
end

puts "ПРОЕКТ=#{project.identifier} id=#{project.id}"
puts "ПОЛЯ стадия=customField#{cf_stage.id} критично=customField#{cf_crit.id} " \
     "входа=customField#{cf_entry.id} возвратов=customField#{cf_back.id}"
puts "ЗАДАЧ=#{made.size} СВЯЗЕЙ=#{Relation.where(from_id: made.values.map(&:id)).count}"
