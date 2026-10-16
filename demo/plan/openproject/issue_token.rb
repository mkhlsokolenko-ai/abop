# Ключ API проектного сервера для агента «Управляющий графиком»: только чтение задач.
#
# Ключ ОБЯЗАН принадлежать живому человеку, который видит проект. Встроенный системный
# пользователь OpenProject тоже `admin = true`, но членства в проектах у него нет, и API отвечает
# «403 MissingPermission» — на этом был потерян час.
#
# Значение ключа никуда не печатается: скрипт кладёт его в файл внутри контейнера, откуда он
# переносится в `/app/secrets/openproject_token` (права 600) и сразу удаляется из /tmp.
#
#   docker cp demo/plan/openproject/issue_token.rb openproject:/tmp/issue_token.rb
#   docker exec openproject bash -lc 'cd /app && RAILS_ENV=production bundle exec rails runner /tmp/issue_token.rb'
#   docker cp openproject:/tmp/op_token.txt /opt/abop/secrets/openproject_token
#   chmod 600 /opt/abop/secrets/openproject_token
#   docker exec openproject rm -f /tmp/op_token.txt /tmp/issue_token.rb
#   docker exec abop-webapi mkdir -p /app/secrets
#   docker cp /opt/abop/secrets/openproject_token abop-webapi:/app/secrets/openproject_token

admin = User.where(admin: true).reject(&:builtin?).first || User.find_by(login: "admin")
raise "не найден живой администратор: ключ встроенного системного пользователя даёт 403" unless admin

Token::API.where(user_id: admin.id).destroy_all
token = Token::API.create!(user: admin)
File.write("/tmp/op_token.txt", token.plain_value)
puts "КЛЮЧ ВЫПИСАН на #{admin.login} (id=#{admin.id}), длина=#{token.plain_value.length}, " \
     "значение в /tmp/op_token.txt — забрать и удалить"
