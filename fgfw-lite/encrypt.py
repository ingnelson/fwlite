#!/usr/bin/env python
# coding: UTF-8
#
# Copyright (c) 2012 clowwindy
# Copyright (C) 2013-2015 Jiang Chao <sgzz.cj@gmail.com>
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, see <http://www.gnu.org/licenses>.

import os
import hashlib
import hmac
import string
from collections import defaultdict, deque
from repoze.lru import lru_cache
from ctypes_libsodium import Salsa20Crypto
try:
    from M2Crypto.EVP import Cipher
    from M2Crypto import EC
    import M2Crypto.Rand
    random_string = M2Crypto.Rand.rand_bytes
except ImportError:
    random_string = os.urandom
    from streamcipher import StreamCipher as Cipher
    EC = None
try:
    from hmac import compare_digest
except ImportError:
    def compare_digest(a, b):
        if isinstance(a, str):
            if len(a) != len(b):
                return False
            result = 0
            for x, y in zip(a, b):
                result |= ord(x) ^ ord(y)
            return result == 0
        else:
            if len(a) != len(b):
                return False
            result = 0
            for x, y in zip(a, b):
                result |= x ^ y
            return result == 0


@lru_cache(128)
def EVP_BytesToKey(password, key_len):
    # equivalent to OpenSSL's EVP_BytesToKey() with count 1
    # so that we make the same key and iv as nodejs version
    m = []
    l = 0
    while l < key_len:
        md5 = hashlib.md5()
        data = password
        if len(m) > 0:
            data = m[len(m) - 1] + password
        md5.update(data)
        m.append(md5.digest())
        l += 16
    ms = b''.join(m)
    return ms[:key_len]


def check(key, method):
    Encryptor(key, method)  # test if the settings if OK

method_supported = {
    'aes-128-cfb': (16, 16),
    'aes-192-cfb': (24, 16),
    'aes-256-cfb': (32, 16),
    'aes-128-ofb': (16, 16),
    'aes-192-ofb': (24, 16),
    'aes-256-ofb': (32, 16),
    'rc4-md5': (16, 16),
    'salsa20': (32, 8),
    'chacha20': (32, 8),
}


def get_cipher_len(method):
    return method_supported.get(method.lower(), None)


class sized_deque(deque):
    def __init__(self):
        deque.__init__(self, maxlen=1048576)

USED_IV = defaultdict(sized_deque)


def create_rc4_md5(method, key, iv, op):
    md5 = hashlib.md5()
    md5.update(key)
    md5.update(iv)
    rc4_key = md5.digest()
    return Cipher('rc4', rc4_key, '', op)


def get_cipher(key, method, op, iv):
    if method == 'rc4-md5':
        return create_rc4_md5(method, key, iv, op)
    elif method in ('salsa20', 'chacha20'):
        return Salsa20Crypto(method, key, iv, op)
    else:
        return Cipher(method.replace('-', '_'), key, iv, op)


class Encryptor(object):
    def __init__(self, password, method=None, servermode=False):
        if method == 'table':
            method = None
        self.key = password
        self.method = method
        self.servermode = servermode
        self.iv_len = 0
        self.iv_sent = False
        self.cipher_iv = b''
        self.decipher = None
        if method is not None:
            self.key_len, self.iv_len = get_cipher_len(method)
            self.key = EVP_BytesToKey(password, self.key_len)
            self.cipher_iv = random_string(self.iv_len)
            self.cipher = get_cipher(self.key, method, 1, self.cipher_iv)
        else:
            raise ValueError('"table" encryption is no longer supported!')

    def encrypt(self, buf):
        if len(buf) == 0:
            raise ValueError('buf should not be empty')
        if self.method is None:
            return string.translate(buf, self.encrypt_table)
        else:
            if self.iv_sent:
                return self.cipher.update(buf)
            else:
                self.iv_sent = True
                return self.cipher_iv + self.cipher.update(buf)

    def decrypt(self, buf):
        if len(buf) == 0:
            raise ValueError('buf should not be empty')
        if self.method is None:
            return string.translate(buf, self.decrypt_table)
        else:
            if self.decipher is None:
                decipher_iv = buf[:self.iv_len]
                if self.servermode:
                    if decipher_iv in USED_IV[self.key]:
                        raise ValueError('iv reused, possible replay attrack')
                    USED_IV[self.key].append(decipher_iv)
                self.decipher = get_cipher(self.key, self.method, 0, decipher_iv)
                buf = buf[self.iv_len:]
                if len(buf) == 0:
                    return buf
            return self.decipher.update(buf)


@lru_cache(128)
def hkdf(key, salt, ctx, key_len):
    '''
    consider key come from a key exchange protocol.
    '''
    key = hmac.new(salt, key, hashlib.sha256).digest()
    sek = hmac.new(key, ctx + b'server_encrypt_key', hashlib.sha256).digest()[:key_len]
    sak = hmac.new(key, ctx + b'server_authenticate_key', hashlib.sha256).digest()
    cek = hmac.new(key, ctx + b'client_encrypt_key', hashlib.sha256).digest()[:key_len]
    cak = hmac.new(key, ctx + b'client_authenticate_key', hashlib.sha256).digest()
    return sek, sak, cek, cak


key_len_to_hash = {
    16: hashlib.md5,
    24: hashlib.sha1,
    32: hashlib.sha256,
}


class AEncryptor(object):
    '''
    Provide Authenticated Encryption
    '''
    def __init__(self, key, method, salt, ctx, servermode):
        if method not in method_supported:
            raise ValueError('method not supported')
        self.method = method
        self.servermode = servermode
        self.key_len, self.iv_len = get_cipher_len(method)
        if servermode:
            self.encrypt_key, self.auth_key, self.decrypt_key, self.de_auth_key = hkdf(key, salt, ctx, self.key_len)
        else:
            self.decrypt_key, self.de_auth_key, self.encrypt_key, self.auth_key = hkdf(key, salt, ctx, self.key_len)
        hfunc = key_len_to_hash[self.key_len]
        self.iv_sent = False
        self.cipher_iv = random_string(self.iv_len)
        self.cipher = get_cipher(self.encrypt_key, method, 1, self.cipher_iv)
        self.decipher = None
        self.enmac = hmac.new(self.auth_key, digestmod=hfunc)
        self.demac = hmac.new(self.de_auth_key, digestmod=hfunc)

    def encrypt(self, buf):
        if len(buf) == 0:
            raise ValueError('buf should not be empty')
        if self.method is None:
            return string.translate(buf, self.encrypt_table)
        else:
            if self.iv_sent:
                ct = self.cipher.update(buf)
            else:
                self.iv_sent = True
                ct = self.cipher_iv + self.cipher.update(buf)
            self.enmac.update(ct)
            return ct, self.enmac.digest()

    def decrypt(self, buf, mac):
        if len(buf) == 0:
            raise ValueError('buf should not be empty')
        self.demac.update(buf)
        rmac = self.demac.digest()
        if self.decipher is None:
            decipher_iv = buf[:self.iv_len]
            if self.servermode:
                if decipher_iv in USED_IV[self.decrypt_key]:
                    raise ValueError('iv reused, possible replay attrack')
                USED_IV[self.decrypt_key].append(decipher_iv)
            self.decipher = get_cipher(self.decrypt_key, self.method, 0, decipher_iv)
            buf = buf[self.iv_len:]
        pt = self.decipher.update(buf) if buf else b''
        if compare_digest(rmac, mac):
            return pt
        raise ValueError('MAC verification failed!')


class ECC(object):
    curve = {256: EC.NID_secp521r1,
             192: EC.NID_secp384r1,
             128: EC.NID_secp256k1,
             32: EC.NID_secp521r1,
             24: EC.NID_secp384r1,
             16: EC.NID_secp256k1,
             }

    def __init__(self, key_len=128, from_file=None):
        if from_file:
            self.ec = EC.load_key(from_file)
        else:
            self.ec = EC.gen_params(self.curve[key_len])
            self.ec.gen_key()

    def get_pub_key(self):
        return self.ec.pub().get_der()[:]

    def get_dh_key(self, otherKey):
        pk = EC.pub_key_from_der(buffer(otherKey))
        return self.ec.compute_dh_key(pk)

    def save(self, dest):
        self.ec.save_key(dest, cipher=None)

    def sign(self, digest):
        '''Sign the given digest using ECDSA. Returns a tuple (r,s), the two ECDSA signature parameters.'''
        return self.ec.sign_dsa(digest)

    def verify(self, digest, r, s):
        '''Verify the given digest using ECDSA. r and s are the ECDSA signature parameters.
           if verified, return 1.
        '''
        return self.ec.verify_dsa(digest, r, s)

    @staticmethod
    def verify_with_pub_key(pubkey, digest, r, s):
        '''Verify the given digest using ECDSA. r and s are the ECDSA signature parameters.
           if verified, return 1.
        '''
        try:
            if isinstance(pubkey, bytes):
                pubkey = EC.pub_key_from_der(buffer(pubkey))
            return pubkey.verify_dsa(digest, r, s)
        except:
            return 0

    @staticmethod
    def save_pub_key(pubkey, dest):
        pubk = EC.pub_key_from_der(buffer(pubkey))
        pubk.save_pub_key(dest)


if __name__ == '__main__':
    print('encrypt and decrypt 20MB data.')
    s = os.urandom(10000)
    import time
    lst = sorted(method_supported.keys())
    for method in lst:
        try:
            cipher = Encryptor('123456', method)
            t = time.time()
            for _ in range(1049):
                a = cipher.encrypt(s)
                b = cipher.encrypt(s)
                c = cipher.decrypt(a)
                d = cipher.decrypt(b)
            print('%s %ss' % (method, time.time() - t))
        except Exception as e:
            print(repr(e))
    print('test AE')
    ae1 = AEncryptor(b'123456', 'aes-256-cfb', 'salt', 'ctx', False, hashlib.sha256)
    ae2 = AEncryptor(b'123456', 'aes-256-cfb', 'salt', 'ctx', True, hashlib.sha256)
    a, b = ae1.encrypt(b'abcde')
    c, d = ae1.encrypt(b'fg')
    print(ae2.decrypt(a, b))
    print(ae2.decrypt(c, d))
    for method in lst:
        try:
            cipher1 = AEncryptor(b'123456', method, 'salt', 'ctx', False, hashlib.md5,)
            cipher2 = AEncryptor(b'123456', method, 'salt', 'ctx', True, hashlib.md5)
            t = time.time()
            for _ in range(1049):
                a, b = cipher1.encrypt(s)
                c, d = cipher1.encrypt(s)
                cipher2.decrypt(a, b)
                cipher2.decrypt(c, d)
            print('%s-HMAC-MD5 %ss' % (method, time.time() - t))
        except Exception as e:
            print(repr(e))
