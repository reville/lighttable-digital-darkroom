import AppKit
import Foundation

// Only the PhotoKit boundary is faked. The production browser's permission,
// pagination, cancellation and event sequencing execute unchanged.
func L(_ value: String) -> String { value }
enum PHAuthorizationStatus { case notDetermined, authorized, limited, denied }
enum PHAccessLevel { case readWrite }
enum PHAssetMediaType: Int { case image = 1 }
enum PHAssetCollectionType { case album, smartAlbum }
enum PHAssetCollectionSubtype { case any, smartAlbumAllHidden, smartAlbumUserLibrary }
enum PHAssetResourceType { case photo, alternatePhoto }
enum PHImageContentMode { case aspectFit }
enum PHImageDeliveryMode { case opportunistic }
enum PHImageResizeMode { case fast }
typealias PHImageRequestID = Int32
class PHChange {}
protocol PHPhotoLibraryChangeObserver: AnyObject { func photoLibraryDidChange(_ changeInstance: PHChange) }
final class PHPhotoLibrary {
    static let instance = PHPhotoLibrary()
    static var status: PHAuthorizationStatus = .denied
    static var authorizationRequests = 0
    static var pendingAuthorization: ((PHAuthorizationStatus) -> Void)?
    weak var observer: PHPhotoLibraryChangeObserver?
    static func shared() -> PHPhotoLibrary { instance }
    static func authorizationStatus(for level: PHAccessLevel) -> PHAuthorizationStatus { status }
    static func requestAuthorization(for level: PHAccessLevel, handler: @escaping (PHAuthorizationStatus) -> Void) {
        authorizationRequests += 1; pendingAuthorization = handler
    }
    func register(_ observer: PHPhotoLibraryChangeObserver) { self.observer = observer }
    func unregisterChangeObserver(_ observer: PHPhotoLibraryChangeObserver) { self.observer = nil }
}
final class PHFetchOptions { var predicate: NSPredicate?; var sortDescriptors: [NSSortDescriptor]? }
final class PHFetchResult<T> {
    let values: [T]
    init(_ values: [T]) { self.values = values }
    var firstObject: T? { values.first }
    var count: Int { values.count }
    func object(at index: Int) -> T { values[index] }
    func enumerateObjects(_ body: (T, Int, UnsafeMutablePointer<ObjCBool>) -> Void) {
        var stop = ObjCBool(false)
        for (i, value) in values.enumerated() { body(value, i, &stop); if stop.boolValue { break } }
    }
}
final class PHAsset {
    static var fetches = 0
    static var fixtures = (0..<125).map { PHAsset("photo-\($0)") }
    let localIdentifier: String
    let pixelWidth = 4000, pixelHeight = 3000
    init(_ id: String) { localIdentifier = id }
    static func fetchAssets(with mediaType: PHAssetMediaType, options: PHFetchOptions) -> PHFetchResult<PHAsset> {
        fetches += 1; return PHFetchResult(fixtures)
    }
    static func fetchAssets(in album: PHAssetCollection, options: PHFetchOptions) -> PHFetchResult<PHAsset> {
        fetches += 1; return PHFetchResult(Array(fixtures.prefix(3)))
    }
}
final class PHAssetResource {
    let type = PHAssetResourceType.photo
    let originalFilename: String
    init(_ name: String) { originalFilename = name }
    static func assetResources(for asset: PHAsset) -> [PHAssetResource] { [PHAssetResource(asset.localIdentifier + ".jpg")] }
}
final class PHAssetCollection {
    let localIdentifier: String
    let localizedTitle: String?
    let assetCollectionSubtype: PHAssetCollectionSubtype
    init(_ id: String, _ subtype: PHAssetCollectionSubtype = .any) {
        localIdentifier = id; localizedTitle = id; assetCollectionSubtype = subtype
    }
    static func fetchAssetCollections(withLocalIdentifiers ids: [String], options: PHFetchOptions?) -> PHFetchResult<PHAssetCollection> {
        PHFetchResult(ids == ["Album"] ? [PHAssetCollection("Album")] : [])
    }
    static func fetchAssetCollections(with type: PHAssetCollectionType, subtype: PHAssetCollectionSubtype, options: PHFetchOptions?) -> PHFetchResult<PHAssetCollection> {
        PHFetchResult(type == .album ? [PHAssetCollection("Album")]
            : [PHAssetCollection("Hidden", .smartAlbumAllHidden), PHAssetCollection("Library", .smartAlbumUserLibrary)])
    }
}
final class PHImageRequestOptions {
    var isNetworkAccessAllowed = false
    var deliveryMode = PHImageDeliveryMode.opportunistic
    var resizeMode = PHImageResizeMode.fast
}
final class PHImageManager {
    static var requested = 0, cancelled = 0
    func cancelImageRequest(_ id: PHImageRequestID) { Self.cancelled += 1 }
    func requestImage(for asset: PHAsset, targetSize: CGSize, contentMode: PHImageContentMode,
                      options: PHImageRequestOptions, resultHandler: @escaping (NSImage?, [String: Any]?) -> Void) -> PHImageRequestID {
        precondition(targetSize == CGSize(width: 240, height: 240))
        Self.requested += 1
        return PHImageRequestID(Self.requested)
    }
}

BROWSER_SOURCE

func wait(_ predicate: () -> Bool) {
    let end = Date().addingTimeInterval(5)
    while !predicate(), Date() < end { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
    precondition(predicate(), "Browser timed out")
}
func drain() { RunLoop.main.run(until: Date().addingTimeInterval(0.1)) }
var events: [[String: Any]] = []
private let browser = ApplePhotosBrowser { events.append($0) }
func request(_ id: Int, _ extra: [String: Any] = [:]) -> [String: Any] {
    browser.page(["requestId": id].merging(extra) { _, new in new })
    wait { events.contains { $0["requestId"] as? Int == id } }
    return events.last { $0["requestId"] as? Int == id }!
}
var result = request(1)
precondition(result["error"] != nil && PHAsset.fetches == 0, "denied access must not read assets")
PHPhotoLibrary.status = .authorized
result = request(2, ["includeAlbums": true])
precondition((result["items"] as? [[String: Any]])?.count == 60, "first page must be bounded")
precondition(result["total"] as? Int == 125 && result["hasMore"] as? Bool == true)
precondition((result["albums"] as? [[String: String]])?.map { $0["id"]! } == ["Album"], "hidden albums must be omitted")
precondition(PHImageManager.requested == 60, "only this page gets thumbnails")
result = request(3, ["offset": 120])
precondition((result["items"] as? [[String: Any]])?.count == 5 && result["hasMore"] as? Bool == false)
precondition(PHImageManager.cancelled == 60, "old thumbnails were not cancelled")
result = request(4, ["offset": 9999])
precondition(result["offset"] as? Int == 120, "last page must clamp after deletion")
result = request(5, ["album": "Album"])
precondition(result["total"] as? Int == 3, "album scope lost")
result = request(6, ["album": "deleted"])
precondition(result["error"] != nil, "deleted albums must not silently become All Photos")
PHPhotoLibrary.status = .limited
result = request(7)
precondition(result["error"] == nil, "permitted subset should be browsable")
PHPhotoLibrary.instance.observer?.photoLibraryDidChange(PHChange())
wait { events.contains { $0["type"] as? String == "applePhotosChanged" } }
browser.close()
precondition(PHPhotoLibrary.instance.observer == nil)
let count = events.count
browser.page(["requestId": 8]); browser.close(); drain()
precondition(events.count == count, "a cancelled request published a stale page")
PHPhotoLibrary.status = .notDetermined
browser.page(["requestId": 9]); browser.close()
PHPhotoLibrary.pendingAuthorization?(.authorized); drain()
precondition(events.count == count, "late permission callback reopened the browser")
PHPhotoLibrary.status = .authorized
PHAsset.fixtures = []
result = request(10)
precondition((result["items"] as? [[String: Any]])?.isEmpty == true && result["total"] as? Int == 0)
browser.close()
print("PASS: permission, bounded pages/thumbnails, album scope, missing albums, limited access, changes, close, stale permission, empty library")
