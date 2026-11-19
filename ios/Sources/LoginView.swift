import SwiftUI

struct LoginView: View {
    @State private var code = ""
    @State private var isLoading = false
    @State private var errorMessage: String?
    @State private var userName: String?
    @State private var token: String?

    private let apiBase = "https://tgbot-naew.onrender.com"

    var body: some View {
        ZStack {
            Color(red: 0.97, green: 0.97, blue: 0.98).ignoresSafeArea()

            VStack(spacing: 24) {
                Spacer()

                ZStack {
                    RoundedRectangle(cornerRadius: 28)
                        .fill(Color.white)
                        .frame(width: 88, height: 88)
                        .shadow(color: .black.opacity(0.06), radius: 16, y: 6)
                    Image(systemName: "bubble.left.and.bubble.right.fill")
                        .font(.system(size: 34, weight: .semibold))
                        .foregroundStyle(Color(red: 0.30, green: 0.48, blue: 0.92))
                }

                VStack(spacing: 8) {
                    Text("Слопчик")
                        .font(.system(size: 34, weight: .bold, design: .rounded))
                        .foregroundStyle(.primary)
                    Text(token == nil ? "Твой личный помощник" : "Вы вошли как \(userName ?? "пользователь")")
                        .font(.system(size: 16))
                        .foregroundStyle(.secondary)
                }

                if token == nil {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Код доступа")
                            .font(.system(size: 14, weight: .semibold))
                            .foregroundStyle(.secondary)
                        SecureField("Введите код из Telegram", text: $code)
                            .textContentType(.oneTimeCode)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .padding(16)
                            .background(Color.white)
                            .clipShape(RoundedRectangle(cornerRadius: 16))
                            .overlay(RoundedRectangle(cornerRadius: 16).stroke(Color.black.opacity(0.06)))
                    }

                    if let errorMessage {
                        Text(errorMessage)
                            .font(.system(size: 13))
                            .foregroundStyle(.red)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }

                    Button(action: login) {
                        HStack {
                            if isLoading { ProgressView().tint(.white) }
                            Text(isLoading ? "Проверяем…" : "Продолжить")
                                .fontWeight(.semibold)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 17)
                        .foregroundStyle(.white)
                        .background(Color(red: 0.30, green: 0.48, blue: 0.92))
                        .clipShape(RoundedRectangle(cornerRadius: 16))
                    }
                    .disabled(code.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || isLoading)
                    .opacity(code.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? 0.55 : 1)
                } else {
                    VStack(spacing: 12) {
                        Label("Авторизация успешна", systemImage: "checkmark.circle.fill")
                            .foregroundStyle(.green)
                        Text("Подключение к чату будет добавлено следующим этапом.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                        Button("Выйти") {
                            token = nil
                            userName = nil
                            code = ""
                        }
                        .foregroundStyle(.blue)
                    }
                }

                Spacer()
                Text("Слопчик • iOS")
                    .font(.footnote)
                    .foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 28)
            .padding(.vertical, 24)
        }
    }

    private func login() {
        guard let url = URL(string: "\(apiBase)/api/login") else { return }
        isLoading = true
        errorMessage = nil

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["code": code.trimmingCharacters(in: .whitespacesAndNewlines)])

        URLSession.shared.dataTask(with: request) { data, response, error in
            DispatchQueue.main.async {
                isLoading = false
                if let error {
                    errorMessage = "Не удалось подключиться: \(error.localizedDescription)"
                    return
                }
                guard let data,
                      let http = response as? HTTPURLResponse,
                      (200...299).contains(http.statusCode),
                      let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let receivedToken = result["token"] as? String else {
                    errorMessage = "Неверный код или сервер временно недоступен."
                    return
                }
                token = receivedToken
                userName = result["name"] as? String ?? "пользователь"
            }
        }.resume()
    }
}
