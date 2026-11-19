import SwiftUI
import PhotosUI
import UniformTypeIdentifiers
import AVFoundation
import AVKit
import Combine

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
    @State private var playbackTime: Double = 0
    @State private var playbackDuration: Double = 0
    @State private var playbackVolume: Double = 1
    @State private var isAudioPlaying = false
    @State private var isAudioLoading = false
    @State private var playbackError: String?
    @State private var expandedAudioID: UUID?
    @State private var isSeekingAudio = false
    @State private var promptText = ""
    @State private var selectedFile: (name: String, mime: String, data: Data)?
    @State private var showAttachmentMenu = false
    @AppStorage("slopchik.theme") private var themeRaw = SlopThemeMode.system.rawValue

    private let apiBase = "https://tgbot-naew.onrender.com"
    private let accent = SlopTheme.accent

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
                .background(Color(uiColor: .secondarySystemGroupedBackground))
            }
            composer
        }
        .background(Color(uiColor: .systemGroupedBackground))
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
                            selectedFile = ("Фото.\(ext)", mime, data)
                            errorMessage = nil
                        }
                        selectedPhoto = nil
                    }
                } catch {
                    await MainActor.run { errorMessage = "Не удалось загрузить фото: \(error.localizedDescription)"; selectedPhoto = nil }
                }
            }
        }
        .onAppear(perform: loadHistory)
        .onReceive(Timer.publish(every: 0.25, on: .main, in: .common).autoconnect()) { _ in
            updatePlaybackProgress()
        }
        .onReceive(NotificationCenter.default.publisher(for: .AVPlayerItemDidPlayToEndTime)) { _ in
            player?.pause()
            player?.seek(to: .zero)
            playbackTime = 0
            isAudioPlaying = false
        }
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
            Menu {
                    Text("Вы вошли как \(userName)")
                    Divider()
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
                    Menu {
                        Button { themeRaw = SlopThemeMode.light.rawValue } label: {
                            Label("Светлая", systemImage: themeRaw == "light" ? "checkmark.circle.fill" : "sun.max")
                        }
                        Button { themeRaw = SlopThemeMode.dark.rawValue } label: {
                            Label("Тёмная", systemImage: themeRaw == "dark" ? "checkmark.circle.fill" : "moon")
                        }
                        Button { themeRaw = SlopThemeMode.system.rawValue } label: {
                            Label("Как в системе", systemImage: themeRaw == "system" ? "checkmark.circle.fill" : "iphone")
                        }
                    } label: {
                        Label("Тема оформления", systemImage: "circle.lefthalf.filled")
                    }
                    Divider()
                    Button("Выйти", role: .destructive, action: onLogout)
                } label: {
                    Image(systemName: "line.3.horizontal")
                        .font(.system(size: 16, weight: .semibold))
                        .frame(width: 40, height: 40)
                        .background(.thinMaterial, in: Circle())
                        .overlay(Circle().stroke(Color.primary.opacity(0.06), lineWidth: 0.8))
                }
                .accessibilityLabel("Меню")
            .buttonStyle(.plain)

            Spacer(minLength: 8)

            VStack(spacing: 1) {
                Text("Слопчик")
                    .font(.system(size: 18, weight: .bold, design: .rounded))
                    .foregroundStyle(.primary)
                Text("AI-помощник")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(.secondary)
            }
            .accessibilityElement(children: .combine)

            Spacer(minLength: 8)

            Button(action: resetChat) {
                Image(systemName: "square.and.pencil")
                    .font(.system(size: 17, weight: .medium))
                    .frame(width: 40, height: 40)
                    .background(.thinMaterial, in: Circle())
                    .overlay(Circle().stroke(Color.primary.opacity(0.06), lineWidth: 0.8))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Новый диалог")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .background(alignment: .bottom) {
            Rectangle()
                .fill(.regularMaterial)
                .frame(height: 42)
                .mask {
                    LinearGradient(
                        stops: [
                            .init(color: .black, location: 0),
                            .init(color: .black, location: 0.22),
                            .init(color: .clear, location: 1)
                        ],
                        startPoint: .top,
                        endPoint: .bottom
                    )
                }
                .offset(y: 42)
                .allowsHitTesting(false)
        }
        .zIndex(1)
    }

    private var composer: some View {
        VStack(spacing: 0) {
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
                .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
                .slopGlass(cornerRadius: 22)
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
        .background(.ultraThinMaterial)
        .slopGlass(cornerRadius: 0)
    }

    private func messageBubble(_ message: ChatMessage) -> some View {
        let isUser = message.role == "user"
        return HStack {
            if isUser { Spacer(minLength: 42) }
            VStack(alignment: .leading, spacing: 9) {
                if !message.text.isEmpty {
                    let rendered = (try? AttributedString(markdown: message.text, options: .init(interpretedSyntax: .full))) ?? AttributedString(message.text)
                    Text(rendered).font(.system(size: 16))
                        .foregroundStyle(isUser ? Color.white : Color.primary)
                        .lineSpacing(4)
                        .textSelection(.enabled)
                }
                ForEach(message.attachments) { attachment in
                    attachmentView(attachment, isUser: isUser)
                }
            }
            .padding(.horizontal, isUser ? 15 : 0).padding(.vertical, isUser ? 11 : 3)
            .background(isUser ? accent : Color.clear)
            .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
            if !isUser { Spacer(minLength: 42) }
        }
        .frame(maxWidth: .infinity)
    }

    @ViewBuilder
    private func attachmentView(_ item: ChatAttachment, isUser: Bool) -> some View {
        if item.type == "image", let data = Data(base64Encoded: item.base64), let image = UIImage(data: data) {
            VStack(alignment: .trailing, spacing: 7) {
                Image(uiImage: image).resizable().scaledToFit().frame(maxWidth: 260, maxHeight: 320)
                    .clipShape(RoundedRectangle(cornerRadius: 13, style: .continuous))
                if let url = temporaryFileURL(item) {
                    ShareLink(item: url) {
                        Label("Поделиться", systemImage: "square.and.arrow.up")
                            .font(.caption.weight(.medium))
                            .foregroundStyle(isUser ? Color.white.opacity(0.9) : accent)
                    }
                }
            }
        } else if item.type == "audio" {
            audioPlayerCard(item)
        } else if item.type == "link", let url = URL(string: item.url) {
            VStack(alignment: .leading, spacing: 6) {
                Link(destination: url) {
                    Label(item.title.isEmpty ? "Открыть ссылку" : item.title, systemImage: "play.rectangle")
                        .font(.subheadline).foregroundStyle(isUser ? .white : accent)
                }
                ShareLink(item: url) {
                    Label("Поделиться", systemImage: "square.and.arrow.up")
                        .font(.caption.weight(.medium))
                        .foregroundStyle(isUser ? Color.white.opacity(0.9) : accent)
                }
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
        let localAttachments: [ChatAttachment] = {
            guard let file else { return [] }
            let isImage = file.mime.hasPrefix("image/")
            return [ChatAttachment(type: isImage ? "image" : "file",
                                   name: file.name, mime: file.mime,
                                   base64: file.data.base64EncodedString(), url: "", title: file.name)]
        }()
        messages.append(ChatMessage(role: "user", text: text, attachments: localAttachments))
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
            guard let url = recording?.url,
                  let data = try? Data(contentsOf: url),
                  !data.isEmpty else {
                errorMessage = "Запись не получилась. Попробуй ещё раз."
                return
            }
            sendVoice(data)
            return
        }

        errorMessage = nil
        let permissionHandler: (Bool) -> Void = { granted in
            DispatchQueue.main.async {
                guard granted else {
                    errorMessage = "Нет доступа к микрофону. Разреши его в настройках iPhone."
                    return
                }
                do {
                    let session = AVAudioSession.sharedInstance()
                    try session.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker])
                    try session.setActive(true)

                    let url = FileManager.default.temporaryDirectory
                        .appendingPathComponent("slopchik-voice-\\(UUID().uuidString).m4a")
                    let settings: [String: Any] = [
                        AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
                        AVSampleRateKey: 44100,
                        AVNumberOfChannelsKey: 1,
                        AVEncoderAudioQualityKey: AVAudioQuality.medium.rawValue
                    ]
                    let recorder = try AVAudioRecorder(url: url, settings: settings)
                    guard recorder.prepareToRecord(), recorder.record() else {
                        throw NSError(domain: "SlopchikAudio", code: 1,
                                      userInfo: [NSLocalizedDescriptionKey: "iPhone не смог запустить запись."])
                    }
                    recording = recorder
                    isRecording = true
                } catch {
                    recording = nil
                    isRecording = false
                    errorMessage = "Не удалось начать запись: \\(error.localizedDescription)"
                }
            }
        }

        if #available(iOS 17.0, *) {
            AVAudioApplication.requestRecordPermission(completionHandler: permissionHandler)
        } else {
            AVAudioSession.sharedInstance().requestRecordPermission(permissionHandler)
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
        let inferredExtension = UTType(mimeType: item.mime)?.preferredFilenameExtension ?? "bin"
        let fileName = (safeName as NSString).pathExtension.isEmpty ? "\(safeName).\(inferredExtension)" : safeName
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(fileName)
        do {
            try data.write(to: url, options: .atomic)
            return url
        } catch {
            return nil
        }
    }

    private func audioPlayerCard(_ item: ChatAttachment) -> some View {
        let isCurrent = playingID == item.id
        let title = item.title.isEmpty ? item.name : item.title
        return VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 11) {
                ZStack {
                    RoundedRectangle(cornerRadius: 13, style: .continuous)
                        .fill(LinearGradient(colors: [accent.opacity(0.95), accent.opacity(0.58)], startPoint: .topLeading, endPoint: .bottomTrailing))
                    Image(systemName: "waveform")
                        .font(.system(size: 23, weight: .semibold))
                        .foregroundStyle(.white)
                }
                .frame(width: 54, height: 54)

                VStack(alignment: .leading, spacing: 3) {
                    Text(title)
                        .font(.system(size: 15, weight: .semibold))
                        .lineLimit(2)
                        .foregroundStyle(.primary)
                    Text(isCurrent && isAudioPlaying ? "Сейчас играет" : "Аудиозапись")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 4)
                Button {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        expandedAudioID = expandedAudioID == item.id ? nil : item.id
                    }
                } label: {
                    Image(systemName: expandedAudioID == item.id ? "chevron.up" : "chevron.down")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(.secondary)
                        .frame(width: 30, height: 34)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(expandedAudioID == item.id ? "Свернуть плеер" : "Развернуть плеер")
            }

            Slider(
                value: Binding(
                    get: { isCurrent ? playbackTime : 0 },
                    set: { value in
                        isSeekingAudio = true
                        playbackTime = value
                        player?.seek(to: CMTime(seconds: value, preferredTimescale: 600))
                        isSeekingAudio = false
                    }
                ),
                in: 0...max(isCurrent ? playbackDuration : 0, 0.1)
            )
            .tint(accent)
            .disabled(!isCurrent || playbackDuration <= 0)

            HStack {
                Text(audioTime(isCurrent ? playbackTime : 0))
                Spacer()
                Text(audioTime(isCurrent ? playbackDuration : 0))
            }
            .font(.system(size: 10, weight: .medium, design: .rounded).monospacedDigit())
            .foregroundStyle(.secondary)

            HStack(spacing: 25) {
                Spacer(minLength: 0)
                Button { seekAudio(by: -15) } label: {
                    Image(systemName: "gobackward.15")
                        .font(.system(size: 21, weight: .regular))
                        .foregroundStyle(.primary)
                }
                .buttonStyle(.plain)
                .disabled(!isCurrent)

                Button { playAudio(item) } label: {
                    Image(systemName: isCurrent && isAudioPlaying ? "pause.fill" : "play.fill")
                        .font(.system(size: 19, weight: .bold))
                        .foregroundStyle(.white)
                        .frame(width: 46, height: 46)
                        .background(accent, in: Circle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(isCurrent && isAudioPlaying ? "Пауза" : "Воспроизвести")

                Button { seekAudio(by: 15) } label: {
                    Image(systemName: "goforward.15")
                        .font(.system(size: 21, weight: .regular))
                        .foregroundStyle(.primary)
                }
                .buttonStyle(.plain)
                .disabled(!isCurrent)

                Spacer(minLength: 0)
            }
            .padding(.top, 1)

            if isCurrent && isAudioLoading {
                HStack(spacing: 7) { ProgressView().tint(accent); Text("Загружаем аудио…").font(.caption).foregroundStyle(.secondary) }
            }
            if isCurrent, let playbackError {
                Text(playbackError).font(.caption).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
            }

            if expandedAudioID == item.id {
                Divider().overlay(Color.primary.opacity(0.06))
                HStack(spacing: 9) {
                    Image(systemName: "speaker.fill").foregroundStyle(.secondary)
                    Slider(value: $playbackVolume, in: 0...1)
                        .tint(accent)
                        .onChange(of: playbackVolume) { value in
                            if isCurrent { player?.volume = Float(value) }
                        }
                    Image(systemName: "speaker.wave.3.fill").foregroundStyle(.secondary)
                }
                .font(.caption)
            }
        }
        .padding(13)
        .frame(maxWidth: 310)
        .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 19, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 19, style: .continuous)
                .stroke(Color.primary.opacity(0.055), lineWidth: 0.8)
        }
    }

    private func playAudio(_ item: ChatAttachment) {
        if playingID == item.id, let player {
            if isAudioPlaying { player.pause(); isAudioPlaying = false }
            else if !isAudioLoading { player.play(); isAudioPlaying = true }
            return
        }
        player?.pause()
        playingID = item.id
        playbackTime = 0
        playbackDuration = 0
        playbackError = nil
        isAudioPlaying = false
        isAudioLoading = true

        if !item.base64.isEmpty {
            guard let data = Data(base64Encoded: item.base64), !data.isEmpty else {
                failAudio("Аудиофайл повреждён или имеет неверный формат.")
                return
            }
            let ext = UTType(mimeType: item.mime)?.preferredFilenameExtension ?? "mp3"
            let localURL = FileManager.default.temporaryDirectory.appendingPathComponent("slopchik-" + UUID().uuidString + "." + ext)
            do { try data.write(to: localURL, options: .atomic); startLocalAudio(localURL) }
            catch { failAudio("Не удалось сохранить аудио: " + error.localizedDescription) }
            return
        }

        guard let remoteURL = URL(string: item.url),
              ["https", "http"].contains(remoteURL.scheme?.lowercased() ?? "") else {
            failAudio("У аудиозаписи нет доступного адреса.")
            return
        }
        URLSession.shared.downloadTask(with: remoteURL) { location, response, error in
            if let error {
                DispatchQueue.main.async { failAudio("Не удалось загрузить аудио: " + error.localizedDescription) }
                return
            }
            guard let location, let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
                let code = (response as? HTTPURLResponse).map { String($0.statusCode) } ?? "нет ответа"
                DispatchQueue.main.async { failAudio("Источник не отдал аудио (HTTP " + code + ").") }
                return
            }
            let mime = http.mimeType ?? ""
            let ext = UTType(mimeType: mime)?.preferredFilenameExtension ?? (remoteURL.pathExtension.isEmpty ? "mp3" : remoteURL.pathExtension)
            let localURL = FileManager.default.temporaryDirectory.appendingPathComponent("slopchik-" + UUID().uuidString + "." + ext)
            do {
                try FileManager.default.moveItem(at: location, to: localURL)
                DispatchQueue.main.async { startLocalAudio(localURL) }
            } catch {
                DispatchQueue.main.async { failAudio("Не удалось подготовить аудио: " + error.localizedDescription) }
            }
        }.resume()
    }

    private func startLocalAudio(_ url: URL) {
        do {
            try AVAudioSession.sharedInstance().setCategory(.playback, mode: .default)
            try AVAudioSession.sharedInstance().setActive(true)
        } catch { failAudio("Не удалось включить звук: " + error.localizedDescription); return }
        let item = AVPlayerItem(url: url)
        let newPlayer = AVPlayer(playerItem: item)
        newPlayer.volume = Float(playbackVolume)
        player = newPlayer
        newPlayer.play()
        isAudioLoading = false
        isAudioPlaying = true
    }

    private func failAudio(_ message: String) {
        player?.pause()
        isAudioLoading = false
        isAudioPlaying = false
        playbackError = message
    }
    private func seekAudio(by seconds: Double) {
        guard let player else { return }
        let target = max(0, min(playbackDuration, playbackTime + seconds))
        player.seek(to: CMTime(seconds: target, preferredTimescale: 600))
        playbackTime = target
    }

    private func updatePlaybackProgress() {
        guard let player, playingID != nil else { return }
        let current = player.currentTime().seconds
        let duration = player.currentItem?.duration.seconds ?? 0
        if current.isFinite && !isSeekingAudio { playbackTime = max(0, current) }
        if duration.isFinite && duration > 0 { playbackDuration = duration }
        if let item = player.currentItem, item.status == .failed {
            failAudio("Формат аудио не удалось воспроизвести: " + (item.error?.localizedDescription ?? "неизвестная ошибка"))
            return
        }
        if player.timeControlStatus == .paused && isAudioPlaying {
            isAudioPlaying = false
        } else if player.timeControlStatus == .playing && !isAudioPlaying {
            isAudioPlaying = true
        }
    }

    private func audioTime(_ seconds: Double) -> String {
        guard seconds.isFinite && seconds >= 0 else { return "0:00" }
        let total = Int(seconds)
        return String(format: "%d:%02d", total / 60, total % 60)
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
