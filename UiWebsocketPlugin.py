from Plugin import PluginManager
from util import SafeRe
import hashlib
import random
import json
import time
import gevent



@PluginManager.registerTo("UiWebsocket")
class UiWebsocketPlugin(object):
    def __init__(self, *args, **kwargs):
        res = super(UiWebsocketPlugin, self).__init__(*args, **kwargs)

        # Automatically join peerReceive
        content_json = self.site.storage.loadJson("content.json")
        if "p2p_filter" in content_json:
            self.channels.append("peerReceive")

            # Flush immediate messages
            for message in self.site.p2p_unread:
                self.cmd("peerReceive", message)
            self.site.p2p_unread = []

        return res


    # Allow to broadcast to any site
    def hasSitePermission(self, address, cmd=None):
        if super(UiWebsocketPlugin, self).hasSitePermission(address, cmd=cmd):
            return True

        return cmd == "peerBroadcast"


    # Broadcast message to other peers
    def actionPeerBroadcast(self, to, message, privatekey=None, peer_count=5, broadcast=True, immediate=False, timeout=60):
        print "peerBroadcast(%r)" % message
        # Check whether P2P messages are supported
        content_json = self.site.storage.loadJson("content.json")
        if "p2p_filter" not in content_json:
            self.response(to, {"error": "Site %s doesn't support P2P messages" % self.site.address})
            return

        # Check whether the message matches passive filter
        if not SafeRe.match(content_json["p2p_filter"], json.dumps(message)):
            self.response(to, {"error": "Invalid message for site %s: %s" % (self.site.address, message)})
            return

        # Not so fast
        if "p2p_freq_limit" in content_json and time.time() - self.site.p2p_last_recv.get("self", 0) < content_json["p2p_freq_limit"]:
            self.response(to, {"error": "Too fast messages"})
            return
        self.site.p2p_last_recv["self"] = time.time()

        # Not so much
        if "p2p_size_limit" in content_json and len(json.dumps(message)) > content_json["p2p_size_limit"]:
            self.response(to, {"error": "Too big message"})
            return


        # Generate message and sign it
        all_message = {
            "message": message,
            "peer_count": peer_count,
            "broadcast": broadcast,
            "immediate": immediate,
            "site": self.site.address
        }
        all_message = json.dumps(all_message)

        nonce = str(random.randint(0, 1000000000))
        msg_hash = hashlib.md5("%s,%s" % (nonce, all_message)).hexdigest()
        signature = self.p2pGetSignature(msg_hash, all_message, privatekey)
        all_message = {
            "raw": all_message,
            "signature": signature,
            "hash": msg_hash
        }

        peers = self.site.getConnectedPeers()
        if len(peers) < peer_count:  # Add more, non-connected peers if necessary
            peers += self.site.getRecentPeers(peer_count - len(peers))

        # Send message to peers
        jobs = []
        for peer in peers:
            jobs.append(gevent.spawn(self.p2pBroadcast, peer, all_message))

        # Send message to myself
        self.site.p2p_received.append(msg_hash)

        websockets = [ws for ws in self.site.websockets if "peerReceive" in ws.channels]
        for ws in websockets:
            ws.cmd("peerReceive", {
                "ip": "self",
                "hash": msg_hash,
                "message": message,
                "signed_by": all_message["signature"].split("|")[0] if all_message["signature"] else ""
            })

        if not websockets and immediate:
            self.site.p2p_unread.append({
                "ip": "self",
                "hash": msg_hash,
                "message": message,
                "signed_by": all_message["signature"].split("|")[0] if all_message["signature"] else ""
            })


        if not broadcast:
            # Makes sense to return result
            res = gevent.joinall(jobs, timeout)
            self.response(to, res)
        else:
            # Reply
            self.response(to, {
                "sent": True
            })

    def p2pBroadcast(self, peer, data):
        print "Broadcast %r to %r" % (data, peer)
        reply = peer.request("peerBroadcast", data)
        if reply is None:
            return {
                "ip": "%s:%s" % (peer.ip, peer.port),
                "reply": {
                    "error": "Connection error"
                }
            }

        return {
            "ip": "%s:%s" % (peer.ip, peer.port),
            "reply": reply
        }

    # Send a message to IP
    def actionPeerSend(self, to, ip, message, privatekey=None):
        print "peerSend(%r, %r)" % (ip, message)
        # Get peer or connect to it if it isn't cached
        peer = self.site.peers.get(ip)
        if not peer:
            mip, mport = ip.split(":")
            peer = self.site.addPeer(mip, mport, source="peerSend")
        if not peer:
            # Couldn't connect to this IP
            self.response(to, {
                "error": "Could not find peer %s" % ip
            })
            return

        print "Use peer %r" % peer

        # Generate hash
        nonce = str(random.randint(0, 1000000000))
        all_message = json.dumps({
            "message": message,
            "site": self.site.address
        })
        msg_hash = hashlib.md5("%s,%s" % (nonce, all_message)).hexdigest()

        # Add singature
        signature = self.p2pGetSignature(msg_hash, all_message, privatekey)

        print "Send %r" % {
            "raw": all_message,
            "signature": signature,
            "hash": msg_hash
        }


        reply = peer.request("peerSend", {
            "raw": all_message,
            "signature": signature,
            "hash": msg_hash
        })
        if "reply" in reply:
            print "Replied: %r" % reply
            self.response(to, reply["reply"])
        else:
            print "Received"



    def p2pGetSignature(self, hash, data, privatekey):
        # Get private key
        if privatekey == "stored":
            # Using site privatekey
            privatekey = self.user.getSiteData(self.site.address).get("privatekey")
        elif not privatekey and privatekey is not None:
            # Using user privatekey
            privatekey = self.user.getAuthPrivatekey(self.site.address)

        # Generate signature
        if privatekey:
            from Crypt import CryptBitcoin
            address = CryptBitcoin.privatekeyToAddress(privatekey)
            return "%s|%s" % (address, CryptBitcoin.sign("%s|%s|%s" % (address, hash, data), privatekey))
        else:
            return ""


    def actionPeerInvalid(self, to, hash):
        self.site.p2p_result[hash].set(False)
    def actionPeerValid(self, to, hash):
        self.site.p2p_result[hash].set(True)

    def actionPeerReply(self, to, hash, reply):
        print "Replied to %s with %s" % (hash, reply)
        self.site.p2p_reply[hash].set(reply)