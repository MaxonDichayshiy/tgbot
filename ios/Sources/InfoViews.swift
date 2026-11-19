import SwiftUI

struct LegalInfoView: View {
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    section("О приложении", """
                    «Слопчик» — независимое приложение-клиент для общения с ботом через сервер проекта. Оно не является официальным приложением Telegram и не связано с Telegram.
                    """)
                    section("Какие данные используются", """
                    Для входа используется одноразовый код, полученный в Telegram. Приложение получает от сервера авторизационный токен и имя профиля. Токен хранится локально на устройстве, чтобы не проходить вход при каждом запуске.

                    Тексты сообщений, выбранные фотографии и документы, а также записи голоса отправляются на сервер проекта для обработки и ответа. Сервер может передавать содержание запроса подключённым внешним сервисам, необходимым для выполнения функции (например, обработки текста, распознавания речи, поиска или создания изображений).
                    """)
                    section("Конфиденциальность", """
                    Не отправляйте через приложение пароли, платёжные данные, документы и другую информацию, которую не хотите передавать для обработки. Не вводите в чат чужие персональные данные без разрешения.

                    Точные сроки хранения данных и журналы работы определяются настройками сервера и используемых внешних сервисов. Это приложение не обещает, что отправленные сообщения и файлы будут немедленно удалены после закрытия чата.
                    """)
                    section("Правила использования", """
                    Используйте приложение законно и уважительно. Не отправляйте материалы, нарушающие права других людей, и не пытайтесь нарушить работу сервера или сторонних сервисов.

                    Ответы бота могут быть неточными. Проверяйте важную информацию по надёжным источникам и не полагайтесь на ответы приложения как на профессиональную юридическую, медицинскую или финансовую консультацию.
                    """)
                    section("Связь", """
                    Вопросы по приложению можно направить создателю через Telegram — раздел «О создателе».
                    """)
                    Text("Редакция информационного раздела: 19 ноября 2026 года.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                .padding(20)
            }
            .background(Color(red: 0.97, green: 0.96, blue: 0.94))
            .navigationTitle("Правовой уголок")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Готово") { dismiss() }
                }
            }
        }
    }

    private func section(_ title: String, _ body: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.headline)
            Text(body).font(.subheadline).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

struct CreatorInfoView: View {
    @Environment(\.dismiss) private var dismiss
    private let accent = Color(red: 0.86, green: 0.36, blue: 0.16)

    var body: some View {
        NavigationStack {
            VStack(spacing: 18) {
                Image(systemName: "person.crop.circle.fill")
                    .font(.system(size: 64)).foregroundStyle(accent)
                VStack(spacing: 6) {
                    Text("Создатель Слопчика")
                        .font(.title2.weight(.bold))
                    Text("Независимый автор проекта")
                        .font(.subheadline).foregroundStyle(.secondary)
                }
                Text("«Слопчик» — самостоятельный проект, который развивается как удобный и простой способ общаться с ботом в отдельном приложении.")
                    .font(.body).multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)
                Link(destination: URL(string: "https://t.me/Maxonkonor")!) {
                    Label("Написать в Telegram", systemImage: "paperplane.fill")
                        .font(.headline).foregroundStyle(.white)
                        .padding(.horizontal, 22).padding(.vertical, 13)
                        .background(accent).clipShape(Capsule())
                }
                Spacer()
            }
            .padding(28).frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color(red: 0.97, green: 0.96, blue: 0.94))
            .navigationTitle("О создателе")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Готово") { dismiss() }
                }
            }
        }
    }
}
