import SwiftUI
import PhotosUI
import UniformTypeIdentifiers
import AVFoundation
import AVKit

private struct ChatAttachment: Identifiable {
    let id = UUID()
    let type: String
    let name: String
    let mime: String
    let base64: String
    let url: String
    let title: String
}

private struct ChatMessage: Identifiable {
    let id = UUID()
    let role: String
    let text: String
    var attachments: [ChatAttachment] = []
}

struct ChatView: View {
    let token: String
    let userName: String
    let onLogout: () -> Void

    @State private var messages: [ChatMessage] = []
    @State private var draft = ""
    @State private var isSending = false
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var showIconSettings = false
    @State private var showLegalInfo = false
    @State private var showCreatorInfo = false
    @FocusState private var isComposerFocused: Bool
    @State private var showPromptSettings = false
    @State private var showFilePicker = false
    @State private var showPhotoPicker = false
    @State private var selectedPhoto: PhotosPickerItem?
    @State private var recording: AVAudioRecorder?
    @State private var isRecording = false
    @State private var player: AVPlayer?
    @State private var playingID: UUID?
    @State private var promptText = ""
    @State private var selectedFile: (name: String, mime: String, data: Data)?
    @State private var showAttachmentMenu = false

    private let apiBase = "https://tgbot-naew.onrender.com"
    private let accent = Color(red: 0.86, green: 0.36, blue: 0.16)

    var body: some View {
        VStack(spacing: 0) {
            header
            if let errorMessage {
                Text(errorMessage).font(.footnote).foregroundStyle(.red)
                    .padding(.horizontal).padding(.vertical, 8)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 14) {
                        if isLoading {
                            ProgressView("Загружаем переписку…").padding(.top, 36)
                        } else if messages.isEmpty {
                            welcome
                        }
                        ForEach(messages) { message in
                            messageBubble(message).id(message.id)
                        }
                    }
                    .padding(.horizontal, 14).padding(.vertical, 18)
                }
                .scrollDismissesKeyboard(.interactively)
                .simultaneousGesture(TapGesture().onEnded { isComposerFocused = false })
                .onChange(of: messages.count) { _ in
                    if let last = messages.last {
                        withAnimation(.easeOut(duration: 0.2)) {
                            proxy.scrollTo(last.id, anchor: .bottom)
                        }
                    }
                }
            }
            if let selectedFile {
                HStack {
                    Image(systemName: selectedFile.mime.hasPrefix("image/") ? "photo" : "doc")
                    Text(selectedFile.name).lineLimit(1)
                    Spacer()
                    Button { self.selectedFile = nil } label: {
                        Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
                    }
                }
                .font(.footnote).padding(.horizontal, 14).padding(.vertical, 8)
                .background(.white)
            }
            composer
        }
        .background(Color(red: 0.97, green: 0.96, blue: 0.94))
        .sheet(isPresented: $showIconSettings) { IconSettingsView() }
        .sheet(isPresented: $showPromptSettings) { promptSheet }
        .sheet(isPresented: $showLegalInfo) { LegalInfoView() }
        .sheet(isPresented: $showCreatorInfo) { CreatorInfoView() }
        .photosPicker(isPresented: $showPhotoPicker, selection: $selectedPhoto, matching: .images)
        .fileImporter(isPresented: $showFilePicker, allowedContentTypes: [.item], allowsMultipleSelection: false, onCompletion: handleFileImport)
        .onChange(of: selectedPhoto) { item in
            guard let item else { return }
            Task {
                do {
                    guard let data = try await item.loadTransferable(type: Data.self) else {
                        await MainActor.run { errorMessage = "Не удалось загрузить выбранное фото."; selectedPhoto = nil }
                        return
                    }
                    let mime = item.supportedContentTypes.first?.preferredMIMEType ?? "image/jpeg"
                    let ext = item.supportedContentTypes.first?.preferredFilenameExtension ?? "jpg"
                    await MainActor.run {
                        if data.count > 9_000_000 {
                            errorMessage = "Фото слишком большое. Максимум — 9 МБ."
                        } else {
                            selectedFile = ("Фото.\\(ext)", mime, data)
                            errorMessage = nil
                        }
                        selectedPhoto = nil
                    }
                } catch {
                    await MainActor.run { errorMessage = "Не удалось загрузить фото: \\(error.localizedDescription)"; selectedPhoto = nil }
                }
            }
        }
        .onAppear(perform: loadHistory)
    }

    private var welcome: some View {
        VStack(spacing: 12) {
            Image(systemName: "bubble.left.and.bubble.right")
                .font(.system(size: 36)).foregroundStyle(accent)
            Text("Слопчик на связи").font(.headline)
            Text("Можно общаться текстом, отправлять фото и документы или записать голосовое сообщение.")
                .font(.subheadline).foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Text("Попробуй: «Нарисуй…», «Погода в…», «Найди в интернете…»")
                .font(.caption).foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(.top, 64).padding(.horizontal, 24)
    }

    private var header: some View {
        HStack(spacing: 12) {
            Image(systemName: "bubble.left.and.bubble.right.fill")
                .font(.system(size: 22)).foregroundStyle(accent)
            VStack(alignment: .leading, spacing: 2) {
                Text("Слопчик").font(.headline)
                Text("Вы вошли как \(userName)").font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button(action: resetChat) {
                Image(systemName: "plus.message").font(.system(size: 18, weight: .medium))
                    .frame(width: 38, height: 38).background(Color.white).clipShape(Circle())
            }
            .accessibilityLabel("Новый диалог")
            Menu {
                Button { showPromptSettings = true } label: {
                    Label("Персональная инструкция", systemImage: "slider.horizontal.3")
                }
                Button { showIconSettings = true } label: {
                    Label("Иконка приложения", systemImage: "app")
                }
                Button { showLegalInfo = true } label: {
                    Label("Правовой уголок", systemImage: "doc.text")
                }
                Button { showCreatorInfo = true } label: {
                    Label("О создателе", systemImage: "person.crop.circle")
                }
                Divider()
                Button("Выйти", role: .destructive, action: onLogout)
            } label: {
                Image(systemName: "ellipsis").font(.system(size: 18, weight: .semibold))
                    .frame(width: 38, height: 38).background(Color.white).clipShape(Circle())
            }
        }
        .padding(.horizontal, 16).padding(.vertical, 12).background(.white)
    }

    private var composer: some View {
        VStack(spacing: 0) {
            if isComposerFocused {
                HStack {
                    Spacer()
                    Button("Готово") { isComposerFocused = false }
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(accent)
                        .padding(.horizontal, 16).padding(.top, 5).padding(.bottom, 2)
                }
            }
            HStack(alignment: .bottom, spacing: 8) {
            Menu {
                Button { showPhotoPicker = true } label: {
                    Label("Выбрать фото", systemImage: "photo")
                }
                Button { showFilePicker = true } label: {
                    Label("Выбрать документ", systemImage: "doc")
                }
            } label: {
                Image(systemName: "plus").font(.system(size: 19, weight: .semibold))
                    .frame(width: 42, height: 46).foregroundStyle(accent)
            }
            TextField("Сообщение…", text: $draft, axis: .vertical)
                .focused($isComposerFocused)
                .submitLabel(.send)
                .lineLimit(1...5).padding(.horizontal, 15).padding(.vertical, 12)
                .background(Color.white).clipShape(RoundedRectangle(cornerRadius: 22))
                .disabled(isSending || isRecording)
            if draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && selectedFile == nil {
                Button(action: toggleRecording) {
                    Image(systemName: isRecording ? "stop.fill" : "mic.fill")
                        .font(.system(size: 17, weight: .semibold))
                        .frame(width: 46, height: 46).foregroundStyle(.white)
                        .background(isRecording ? Color.red : accent).clipShape(Circle())
                }
                .disabled(isSending)
                .accessibilityLabel(isRecording ? "Остановить запись" : "Записать голос")
            } else {
                Button(action: sendMessage) {
                    Group {
                        if isSending { ProgressView().tint(.white) }
                        else { Image(systemName: "arrow.up").font(.system(size: 17, weight: .bold)) }
                    }
                    .frame(width: 46, height: 46).foregroundStyle(.white)
                    .background(accent).clipShape(Circle())
                }
                .disabled(isSending)
            }
            }
            .padding(.horizontal, 10).padding(.vertical, 10)
        }
        .background(Color(red: 0.97, green: 0.96, blue: 0.94))
    }

    private func messageBubble(_ message: ChatMessage) -> some View {
        let isUser = message.role == "user"
        return HStack {
            if isUser { Spacer(minLength: 42) }
            VStack(alignment: .leading, spacing: 9) {
                if !message.text.isEmpty {
                    Text(message.text).font(.system(size: 16))
                        .foregroundStyle(isUser ? Color.white : Color.primary)
                        .textSelection(.enabled)
                }
                ForEach(message.attachments) { attachment in
                    attachmentView(attachment, isUser: isUser)
                }
            }
            .padding(.horizontal, 15).padding(.vertical, 11)
            .background(isUser ? accent : Color.white)
            .clipShape(RoundedRectangle(cornerRadius: 18))
            if !isUser { Spacer(minLength: 42) }
        }
        .frame(maxWidth: .infinity)
    }

    @ViewBuilder
    private func attachmentView(_ item: ChatAttachment, isUser: Bool) -> some View {
        if item.type == "image", let data = Data(base64Encoded: item.base64), let image = UIImage(data: data) {
            Image(uiImage: image).resizable().scaledToFit().frame(maxWidth: 240, maxHeight: 280)
                .clipShape(RoundedRectangle(cornerRadius: 12))
        } else if item.type == "audio" {
            Button {
                playAudio(item)
            } label: {
                Label(playingID == item.id ? "Воспроизводится…" : (item.title.isEmpty ? "Прослушать аудио" : item.title),
                      systemImage: playingID == item.id ? "speaker.wave.2.fill" : "play.circle.fill")
                    .font(.subheadline)
                    .foregroundStyle(isUser ? .white : accent)
            }
        } else if item.type == "link", let url = URL(string: item.url) {
            Link(destination: url) {
                Label(item.title.isEmpty ? "Открыть ссылку" : item.title, systemImage: "play.rectangle")
                    .font(.subheadline).foregroundStyle(isUser ? .white : accent)
            }
        } else if item.type == "file", let url = temporaryFileURL(item) {
            ShareLink(item: url) {
                Label("Сохранить \(item.name)", systemImage: "doc")
                    .font(.subheadline).foregroundStyle(isUser ? .white : accent)
            }
        }
    }

    private func authorizedRequest(_ url: URL, method: String = "GET") -> URLRequest {
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        return request
    }

    private func loadHistory() {
        guard let url = URL(string: "\(apiBase)/api/history") else { return }
        var request = authorizedRequest(url)
        request.timeoutInterval = 30
        URLSession.shared.dataTask(with: request) { data, response, error in
            DispatchQueue.main.async {
                isLoading = false
                if let error {
                    errorMessage = "Не удалось загрузить историю: \(error.localizedDescription)"
                    return
                }
                guard let data, let http = response as? HTTPURLResponse, http.statusCode == 200,
                      let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let items = result["messages"] as? [[String: Any]] else {
                    errorMessage = "Не удалось получить историю. Попробуйте перезапустить приложение."
                    return
                }
                messages = items.compactMap { item in
                    guard let role = item["role"] as? String,
                          let text = item["text"] as? String, !text.isEmpty else { return nil }
                    return ChatMessage(role: role, text: text)
                }
            }
        }.resume()
    }

    private func sendMessage() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard (!text.isEmpty || selectedFile != nil), let url = URL(string: "\(apiBase)/api/chat") else { return }
        let file = selectedFile
        draft = ""
        selectedFile = nil
        isSending = true
        errorMessage = nil
        messages.append(ChatMessage(role: "user", text: text.isEmpty ? (file?.name ?? "") : text))
        var body: [String: Any] = ["text": text]
        if let file {
            body["file"] = ["name": file.name, "mime": file.mime, "b64": file.data.base64EncodedString()]
        }
        var request = authorizedRequest(url, method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 180
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: request) { data, response, error in
            DispatchQueue.main.async {
                isSending = false
                if let error {
                    errorMessage = "Не удалось отправить: \(error.localizedDescription)"
                    return
                }
                guard let data, let http = response as? HTTPURLResponse,
                      let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    errorMessage = "Сервер не ответил. Попробуйте ещё раз."
                    return
                }
                guard http.statusCode == 200 else {
                    errorMessage = result["error"] as? String ?? "Ошибка сервера."
                    return
                }
                let answer = result["text"] as? String ?? ""
                let attachments = (result["attachments"] as? [[String: Any]] ?? []).map { value in
                    ChatAttachment(type: value["type"] as? String ?? "",
                                   name: value["name"] as? String ?? "Файл",
                                   mime: value["mime"] as? String ?? "",
                                   base64: value["b64"] as? String ?? "",
                                   url: value["url"] as? String ?? "",
                                   title: value["title"] as? String ?? "")
                }
                if !answer.isEmpty || !attachments.isEmpty {
                    messages.append(ChatMessage(role: "assistant", text: answer, attachments: attachments))
                }
            }
        }.resume()
    }

    private func handleFileImport(_ result: Result<[URL], Error>) {
        do {
            guard let url = try result.get().first else { return }
            let access = url.startAccessingSecurityScopedResource()
            defer { if access { url.stopAccessingSecurityScopedResource() } }
            let data = try Data(contentsOf: url)
            guard data.count <= 9_000_000 else {
                errorMessage = "Файл слишком большой. Максимум — 9 МБ."
                return
            }
            selectedFile = (url.lastPathComponent, mimeType(for: url), data)
        } catch {
            errorMessage = "Не удалось открыть файл: \(error.localizedDescription)"
        }
    }

    private func mimeType(for url: URL) -> String {
        switch url.pathExtension.lowercased() {
        case "pdf": return "application/pdf"
        case "docx": return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        case "xlsx": return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        case "csv": return "text/csv"
        case "json": return "application/json"
        case "txt": return "text/plain"
        case "html", "htm": return "text/html"
        case "png": return "image/png"
        case "jpg", "jpeg": return "image/jpeg"
        case "gif": return "image/gif"
        default: return "application/octet-stream"
        }
    }

    private func toggleRecording() {
        if isRecording {
            recording?.stop()
            isRecording = false
            guard let url = recording?.url, let data = try? Data(contentsOf: url) else { return }
            sendVoice(data)
            return
        }
        AVAudioSession.sharedInstance().requestRecordPermission { granted in
            DispatchQueue.main.async {
                guard granted else {
                    errorMessage = "Разреши доступ к микрофону в настройках iPhone."
                    return
                }
                do {
                    let session = AVAudioSession.sharedInstance()
                    try session.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker])
                    try session.setActive(true)
                    let url = FileManager.default.temporaryDirectory.appendingPathComponent("slopchik-voice-\(UUID().uuidString).m4a")
                    let settings: [String: Any] = [
                        AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
                        AVSampleRateKey: 44100,
                        AVNumberOfChannelsKey: 1,
                        AVEncoderAudioQualityKey: AVAudioQuality.medium.rawValue
                    ]
                    recording = try AVAudioRecorder(url: url, settings: settings)
                    recording?.record()
                    isRecording = true
                } catch {
                    errorMessage = "Не удалось начать запись: \(error.localizedDescription)"
                }
            }
        }
    }

    private func sendVoice(_ data: Data) {
        guard let url = URL(string: "\(apiBase)/api/voice") else { return }
        isSending = true
        messages.append(ChatMessage(role: "user", text: "🎙 Голосовое сообщение"))
        var request = authorizedRequest(url, method: "POST")
        request.setValue("audio/mp4", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 180
        request.httpBody = data
        URLSession.shared.dataTask(with: request) { data, response, error in
            DispatchQueue.main.async {
                isSending = false
                if let error {
                    errorMessage = "Не удалось отправить голос: \(error.localizedDescription)"
                    return
                }
                guard let data, let http = response as? HTTPURLResponse,
                      let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      http.statusCode == 200 else {
                    errorMessage = "Не удалось распознать голосовое сообщение."
                    return
                }
                let heard = result["heard"] as? String ?? ""
                let answer = result["text"] as? String ?? ""
                if !heard.isEmpty { messages.append(ChatMessage(role: "user", text: "🎙 \(heard)")) }
                if !answer.isEmpty { messages.append(ChatMessage(role: "assistant", text: answer)) }
            }
        }.resume()
    }

    private func temporaryFileURL(_ item: ChatAttachment) -> URL? {
        guard let data = Data(base64Encoded: item.base64) else { return nil }
        let safeName = item.name.replacingOccurrences(of: "/", with: "_")
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(safeName)
        do {
            try data.write(to: url, options: .atomic)
            return url
        } catch {
            return nil
        }
    }

    private func playAudio(_ item: ChatAttachment) {
        if !item.base64.isEmpty, let data = Data(base64Encoded: item.base64) {
            let url = FileManager.default.temporaryDirectory.appendingPathComponent("slopchik-audio-\(item.id).mp3")
            try? data.write(to: url)
            player = AVPlayer(url: url)
            playingID = item.id
            player?.play()
        } else if let url = URL(string: item.url) {
            player = AVPlayer(url: url)
            playingID = item.id
            player?.play()
        }
    }

    private var promptSheet: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 14) {
                Text("Задай Слопчику постоянные инструкции: стиль общения, предпочтения или правила ответов.")
                    .font(.subheadline).foregroundStyle(.secondary)
                TextEditor(text: $promptText)
                    .padding(8).background(Color.white)
                    .clipShape(RoundedRectangle(cornerRadius: 14))
                Button("Сохранить инструкцию") { savePrompt() }
                    .fontWeight(.semibold).frame(maxWidth: .infinity).padding()
                    .foregroundStyle(.white).background(accent)
                    .clipShape(RoundedRectangle(cornerRadius: 14))
            }
            .padding().background(Color(red: 0.97, green: 0.96, blue: 0.94))
            .navigationTitle("Инструкция").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Готово") { showPromptSettings = false }
                }
            }
            .onAppear(perform: loadPrompt)
        }
    }

    private func loadPrompt() {
        guard let url = URL(string: "\(apiBase)/api/prompt") else { return }
        var request = authorizedRequest(url, method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["get": true])
        URLSession.shared.dataTask(with: request) { data, _, _ in
            if let data, let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                DispatchQueue.main.async { promptText = result["text"] as? String ?? "" }
            }
        }.resume()
    }

    private func savePrompt() {
        guard let url = URL(string: "\(apiBase)/api/prompt") else { return }
        var request = authorizedRequest(url, method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["text": promptText])
        URLSession.shared.dataTask(with: request) { data, response, error in
            DispatchQueue.main.async {
                if let error {
                    errorMessage = "Не удалось сохранить инструкцию: \(error.localizedDescription)"
                    return
                }
                guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                    errorMessage = "Не удалось сохранить инструкцию."
                    return
                }
                showPromptSettings = false
            }
        }.resume()
    }

    private func resetChat() {
        guard let url = URL(string: "\(apiBase)/api/reset") else { return }
        var request = authorizedRequest(url, method: "POST")
        request.timeoutInterval = 30
        URLSession.shared.dataTask(with: request) { _, response, error in
            DispatchQueue.main.async {
                if let error {
                    errorMessage = "Не удалось начать новый диалог: \(error.localizedDescription)"
                    return
                }
                guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                    errorMessage = "Не удалось очистить переписку."
                    return
                }
                messages = []
                errorMessage = nil
            }
        }.resume()
    }
}
