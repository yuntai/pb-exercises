from binascii import hexlify, unhexlify
from io import BytesIO
from unittest import TestCase

import random
import requests

from ecc import PrivateKey, S256Point, Signature
from helper import (
    decode_base58,
    double_sha256,
    encode_varint,
    int_to_little_endian,
    little_endian_to_int,
    p2pkh_script,
    read_varint,
    SIGHASH_ALL,
)
from script import Script

class Tx:
    def __init__(self, version, tx_ins, tx_outs, locktime, *, witnesses=None, flag=1, testnet=False):
        self.version = version
        self.tx_ins = tx_ins
        self.tx_outs = tx_outs
        self.locktime = locktime
        self.testnet = testnet
        self.witnesses = witnesses
        self.flag = flag

    def txid(self):
        """ non-witness serialization for txid"""
        return double_sha256(self.nw_serialize())[::-1]

    def hash(self):
        """ witness id """
        return double_sha256(self.serialize())[::-1]


    def __repr__(self):
        tx_ins = ''
        for tx_in in self.tx_ins:
            tx_ins += tx_in.__repr__() + '\n'
        tx_outs = ''
        for tx_out in self.tx_outs:
            tx_outs += tx_out.__repr__() + '\n'
        return 'version: {}\ntx_ins:\n{}\ntx_outs:\n{}\nlocktime: {}\n'.format(
            self.version,
            tx_ins,
            tx_outs,
            self.locktime,
        )

    @classmethod
    def parse(cls, s):
        '''Takes a byte stream and parses the transaction at the start
        return a Tx object
        '''
        # s.read(n) will return n bytes
        # version has 4 bytes, little-endian, interpret as int
        version = little_endian_to_int(s.read(4))

        marker = s.read(1)[0]
        if marker != 0:
            raise RuntimeError('marker must be zero')

        flag = s.read(1)[0]
        if flag == 0:
            raise RuntimeError('flag must be nonzero')

        # num_inputs is a varint, use read_varint(s)
        num_inputs = read_varint(s)
        # each input needs parsing
        inputs = []
        for _ in range(num_inputs):
            inputs.append(TxIn.parse(s))
        # num_outputs is a varint, use read_varint(s)
        num_outputs = read_varint(s)
        # each output needs parsing
        outputs = []
        for _ in range(num_outputs):
            outputs.append(TxOut.parse(s))

        # no of script witnesses implied by txin_count
        witnesses = []
        for _ in range(num_inputs):
            num_witness = read_varint(s)
            stack = [] # stack of bytes array
            for _ in range(num_witness):
                sz_witness = read_varint(s)
                stack.append(s.read(sz_witness))
            witnesses.append(stack)

        # locktime is 4 bytes, little-endian
        locktime = little_endian_to_int(s.read(4))
        # return an instance of the class (cls(...))
        return cls(version, inputs, outputs, locktime, witnesses=witnesses, flag=1)

    def nw_serialize(self): # non-witness serialize
        '''Returns the byte serialization of the transaction'''
        # serialize version (4 bytes, little endian)
        result = int_to_little_endian(self.version, 4)

        # encode_varint on the number of inputs
        result += encode_varint(len(self.tx_ins))
        # iterate inputs
        for tx_in in self.tx_ins:
            # serialize each input
            result += tx_in.serialize()
        # encode_varint on the number of inputs
        result += encode_varint(len(self.tx_outs))
        # iterate outputs
        for tx_out in self.tx_outs:
            # serialize each output
            result += tx_out.serialize()

        # serialize locktime (4 bytes, little endian)
        result += int_to_little_endian(self.locktime, 4)
        return result

    def serialize(self):
        '''Returns the byte serialization of the transaction'''
        # serialize version (4 bytes, little endian)
        result = int_to_little_endian(self.version, 4)

        result += b'\x00'         # marker
        result += bytes([self.flag])

        # encode_varint on the number of inputs
        result += encode_varint(len(self.tx_ins))
        # iterate inputs
        for tx_in in self.tx_ins:
            # serialize each input
            result += tx_in.serialize()
        # encode_varint on the number of inputs
        result += encode_varint(len(self.tx_outs))
        # iterate outputs
        for tx_out in self.tx_outs:
            # serialize each output
            result += tx_out.serialize()

        # iterate witnesses for each input
        for ix in range(len(self.tx_ins)):
            result += encode_varint(len(self.witnesses[ix]))
            # iterate witnesses
            for w in self.witnesses[ix]:
                # serialize each output
                result += encode_varint(len(w))
                result += w

        # serialize locktime (4 bytes, little endian)
        result += int_to_little_endian(self.locktime, 4)
        return result

    def fee(self):
        '''Returns the fee of this transaction in satoshi'''
        # initialize input sum and output sum
        input_sum, output_sum = 0, 0
        # iterate through inputs
        for tx_in in self.tx_ins:
            # for each input get the value and add to input sum
            input_sum += tx_in.value()
        # iterate through outputs
        for tx_out in self.tx_outs:
            # for each output get the amount and add to output sum
            output_sum += tx_out.amount
        # return input sum - output sum
        return input_sum - output_sum

    def sig_hash(self, input_index, hash_type):
        '''Returns the integer representation of the hash that needs to get
        signed for index input_index'''
        # create a new set of tx_ins (alt_tx_ins)
        alt_tx_ins = []
        # iterate over self.tx_ins
        for tx_in in self.tx_ins:
            # create a new TxIn that has a blank script_sig (b'') and add to alt_tx_ins
            alt_tx_ins.append(TxIn(
                prev_tx=tx_in.prev_tx,
                prev_index=tx_in.prev_index,
                script_sig=b'',
                sequence=tx_in.sequence,
            ))
        # grab the input at the input_index
        signing_input = alt_tx_ins[input_index]
        # grab the script_pubkey of the input
        script_pubkey = signing_input.script_pubkey(self.testnet)
        # Exercise 6.2: get the sig type from script_pubkey.type()
        sig_type = script_pubkey.type()
        # Exercise 6.2: the script_sig of the signing_input should be script_pubkey for p2pkh
        if sig_type == 'p2pkh':
            # Exercise 6.2: replace the input's scriptSig with the scriptPubKey
            signing_input.script_sig = script_pubkey
        # Exercise 6.2: the script_sig of the signing_input should be the redeemScript
        #               of the current input of the real tx_in (self.tx_ins[input_index].redeem_script()
        elif sig_type == 'p2sh':
            # Exercise 6.2: replace the input's scriptSig with the RedeemScript
            current_input = self.tx_ins[input_index]
            # Exercise 6.2: replace the input's scriptSig with the Script.parse(redeem_script)
            signing_input.script_sig = Script.parse(
                current_input.redeem_script())
        else:
            raise RuntimeError('no valid sig_type')
        # create an alternate transaction with the modified tx_ins
        alt_tx = self.__class__(
            version=self.version,
            tx_ins=alt_tx_ins,
            tx_outs=self.tx_outs,
            locktime=self.locktime)
        # add the hash_type int 4 bytes, little endian
        result = alt_tx.serialize() + int_to_little_endian(hash_type, 4)
        # get the double_sha256 of the tx serialization
        s256 = double_sha256(result)
        # convert this to a big-endian integer using int.from_bytes(x, 'big')
        return int.from_bytes(s256, 'big')

    def verify_input(self, input_index):
        '''Returns whether the input has a valid signature'''
        # Exercise 1.1: get the relevant input
        tx_in = self.tx_ins[input_index]
        # Exercise 6.2: get the number of signatures required. This is available in tx_in.script_sig.num_sigs_required()
        sigs_required = tx_in.script_sig.num_sigs_required()
        # Exercise 6.2: iterate over the sigs required and check each signature
        for sig_num in range(sigs_required):
            # Exercise 1.1: get the point from the sec format (tx_in.sec_pubkey())
            # Exercise 6.2: get the sec_pubkey at current signature index (check sec_pubkey function)
            point = S256Point.parse(tx_in.sec_pubkey(index=sig_num))
            # Exercise 1.1: get the der sig and hash_type from input
            # Exercise 6.2: get the der_signature at current signature index (check der_signature function)
            der, hash_type = tx_in.der_signature(index=sig_num)
            # Exercise 1.1: get the signature from der format
            signature = Signature.parse(der)
            # Exercise 1.1: get the hash to sign
            z = self.sig_hash(input_index, hash_type)
            # Exercise 1.1: use point.verify on the hash to sign and signature
            if not point.verify(z, signature):
                return False
        return True

    def sign_input(self, input_index, private_key, hash_type):
        '''Signs the input using the private key'''
        # get the hash to sign
        z = self.sig_hash(input_index, hash_type)
        # get der signature of z from private key
        der = private_key.sign(z).der()
        # append the hash_type to der (use bytes([hash_type]))
        sig = der + bytes([hash_type])
        # calculate the sec
        sec = private_key.point.sec()
        # initialize a new script with [sig, sec] as the elements
        script_sig = Script([sig, sec])
        # change input's script_sig to new script
        self.tx_ins[input_index].script_sig = script_sig
        # return whether sig is valid using self.verify_input
        return self.verify_input(input_index)

    def is_coinbase(self):
        '''Returns whether this transaction is a coinbase transaction or not'''
        # check that there is exactly 1 input
        if len(self.tx_ins) != 1:
            return False
        # grab the first input
        first_input = self.tx_ins[0]
        # check that first input prev_tx is b'\x00' * 32 bytes
        if first_input.prev_tx != b'\x00' * 32:
            return False
        # check that first input prev_index is 0xffffffff
        if first_input.prev_index != 0xffffffff:
            return False
        return True

    def coinbase_height(self):
        '''Returns the height of the block this coinbase transaction is in
        Returns None if this transaction is not a coinbase transaction
        '''
        # if this is NOT a coinbase transaction, return None
        if not self.is_coinbase():
            return None
        # grab the first input
        first_input = self.tx_ins[0]
        # grab the first element of the script_sig (.script_sig.elements[0])
        first_element = first_input.script_sig.elements[0]
        # convert the first element from little endian to int
        return little_endian_to_int(first_element)

class TxIn:

    cache = {}

    def __init__(self, prev_tx, prev_index, script_sig, sequence=4294967295):
        self.prev_tx = prev_tx
        self.prev_index = prev_index
        self.script_sig = Script.parse(script_sig)
        self.sequence = sequence

    def __repr__(self):
        return '{}:{}'.format(
            hexlify(self.prev_tx).decode('ascii'),
            self.prev_index,
        )

    def nsequence(self):
        # disable flag, type flag, masked value
        disable_flag = 1<<31 & self.sequence
        if not disable_flag:
            type_flag = 1<<22 & self.sequence
            val = 0x0000FFFF&self.sequence
            if type_flag:
                return (True, 'sec', val * 512)
            else:
                return (True, 'block', val)

    @classmethod
    def parse(cls, s):
        '''Takes a byte stream and parses the tx_input at the start
        return a TxIn object
        '''
        # s.read(n) will return n bytes
        # prev_tx is 32 bytes, little endian
        prev_tx = s.read(32)[::-1]
        # prev_index is 4 bytes, little endian, interpret as int
        prev_index = little_endian_to_int(s.read(4))
        # script_sig is a variable field (length followed by the data)
        # get the length by using read_varint(s)
        script_sig_length = read_varint(s)
        script_sig = s.read(script_sig_length)
        # sequence is 4 bytes, little-endian, interpret as int
        sequence = little_endian_to_int(s.read(4))
        # return an instance of the class (cls(...))
        return cls(prev_tx, prev_index, script_sig, sequence)

    def serialize(self):
        '''Returns the byte serialization of the transaction input'''
        # serialize prev_tx, little endian
        result = self.prev_tx[::-1]
        # serialize prev_index, 4 bytes, little endian
        result += int_to_little_endian(self.prev_index, 4)
        # get the scriptSig ready (use self.script_sig.serialize())
        raw_script_sig = self.script_sig.serialize()
        # encode_varint on the length of the scriptSig
        result += encode_varint(len(raw_script_sig))
        # add the scriptSig
        result += raw_script_sig
        # serialize sequence, 4 bytes, little endian
        result += int_to_little_endian(self.sequence, 4)
        return result

    @classmethod
    def get_url(cls, testnet=False):
        if testnet:
            return 'https://testnet.blockexplorer.com/api'
        else:
            return 'https://btc-bitcore3.trezor.io/api'

    def fetch_tx(self, testnet=False):
        if self.prev_tx not in self.cache:
            url = self.get_url(testnet) + '/rawtx/{}'.format(hexlify(self.prev_tx).decode('ascii'))
            response = requests.get(url)
            js_response = response.json()
            if 'rawtx' not in js_response:
                raise RuntimeError('got from server: {}'.format(js_response))
            raw = unhexlify(js_response['rawtx'])
            stream = BytesIO(raw)
            tx = Tx.parse(stream)
            self.cache[self.prev_tx] = tx
        return self.cache[self.prev_tx]

    def value(self, testnet=False):
        '''Get the outpoint value by looking up the tx hash on libbitcoin server
        Returns the amount in satoshi
        '''
        # use self.fetch_tx to get the transaction
        tx = self.fetch_tx(testnet=testnet)
        # get the output at self.prev_index
        # return the amount property
        return tx.tx_outs[self.prev_index].amount

    def script_pubkey(self, testnet=False):
        '''Get the scriptPubKey by looking up the tx hash on libbitcoin server
        Returns the binary scriptpubkey
        '''
        # use self.fetch_tx to get the transaction
        tx = self.fetch_tx(testnet=testnet)
        # get the output at self.prev_index
        # return the script_pubkey property and serialize
        return tx.tx_outs[self.prev_index].script_pubkey

    def der_signature(self, index=0):
        '''returns a DER format signature and hash_type if the script_sig
        has a signature'''
        signature = self.script_sig.der_signature(index=index)
        # last byte is the hash_type, rest is the signature
        return signature[:-1], signature[-1]

    def sec_pubkey(self, index=0):
        '''returns the SEC format public if the script_sig has one'''
        return self.script_sig.sec_pubkey(index=index)

    def redeem_script(self):
        '''return the Redeem Script if there is one'''
        return self.script_sig.redeem_script()


class TxOut:

    def __init__(self, amount, script_pubkey):
        self.amount = amount
        self.script_pubkey = Script.parse(script_pubkey)

    def __repr__(self):
        return '{}:{}'.format(self.amount, self.script_pubkey.address())

    @classmethod
    def parse(cls, s):
        '''Takes a byte stream and parses the tx_output at the start
        return a TxOut object
        '''
        # s.read(n) will return n bytes
        # amount is 8 bytes, little endian, interpret as int
        amount = little_endian_to_int(s.read(8))
        # script_pubkey is a variable field (length followed by the data)
        # get the length by using read_varint(s)
        script_pubkey_length = read_varint(s)
        script_pubkey = s.read(script_pubkey_length)
        # return an instance of the class (cls(...))
        return cls(amount, script_pubkey)

    def serialize(self):
        '''Returns the byte serialization of the transaction output'''
        # serialize amount, 8 bytes, little endian
        result = int_to_little_endian(self.amount, 8)
        # get the scriptPubkey ready (use self.script_pubkey.serialize())
        raw_script_pubkey = self.script_pubkey.serialize()
        # encode_varint on the length of the scriptPubkey
        result += encode_varint(len(raw_script_pubkey))
        # add the scriptPubKey
        result += raw_script_pubkey
        return result


class TxTest(TestCase):
    #bitcoin-cli getrawtransaction 8daadcbf5bac325f9b8d7812d82fcca4dd5a594c9f3c7e80727c565ef87e7b73 1|jq ".hex"
    raw_tx_hex = '0100000000010243d652c3100a5523c644aed493a44b0ccf63b01500771876a8731ea7da6eb8120300000000ffffffff8fad638285b65e974fbcf27067d48651bdbbaf5ae5162cf89831dbc15ec260380400000000ffffffff04801a0600000000001976a914a856a8ebb8aeb82bbec88cabd398e6d1690690ad88acf0874b000000000017a91469f37704405df58ee210a89f24ae6a67a7ffe71f8700562183000000001976a914d8ff5d59bc4bba23546250e533cc01c0ee21883a88ac90e7280500000000220020701a8d401c84fb13e6baf169d59684e17abd9fa216c8cc5b9fc63d622ff8c58d04004830450221009d9ab1b126e25631c20c444f46c600d794c4aa8b4c864a3e664967f4c6ecc73b022019201da2fa3dc67d37817fbb3b91d160993892cbb1a0b335a8f61889871f39f10147304402201f6692696d35c615df6273daf60600f4b1b3f2e7d52610f791bcdc551eaf581a022001869225fc6069e9c37cdf3ad702a4d87c4ffa1a6b779f468cd15f78729a4e61016952210266edd4ef2953675faf0662c088a7f620935807d200d65387290b31648e51e253210372ce38027ee95c98cdc54172964fa3aecf9f24b85c139d3d203365d6b691d0502103c96d495bfdd5ba4145e3e046fee45e84a8a48ad05bd8dbb395c011a32cf9f88053ae0400483045022100fc8f367e892acc7a85b26e404005160d8bf0e3dcd87ad0e1dc40744780b5d42602206ae3e6fed3fbc1bd57631bd51c079f1a054f6e44bc9b84be37fa231ff3c0808401473044022007221455aae1bb958c3d08e51cd5aeddf348336c13893176c94ee3289262e570022009119f5878a310fdc530432bd87f47b700cabc6c8b8ac9e5d19ac5795472ef08016952210375e00eb72e29da82b89367947f29ef34afb75e8654f6ea368e0acdfd92976b7c2103a1b26313f430c4b15bb1fdce663207659d8cac749a0e53d70eff01874496feff2103c96d495bfdd5ba4145e3e046fee45e84a8a48ad05bd8dbb395c011a32cf9f88053ae00000000'

    def test_parse_version(self):
        tx = Tx.parse(BytesIO(unhexlify(TxTest.raw_tx_hex)))
        self.assertEqual(tx.version, 1)

    def test_parse_inputs(self):
        tx = Tx.parse(BytesIO(unhexlify(TxTest.raw_tx_hex)))
        #bitcoin-cli getrawtransaction 8daadcbf5bac325f9b8d7812d82fcca4dd5a594c9f3c7e80727c565ef87e7b73 1|jq ".vin|length"
        self.assertEqual(len(tx.tx_ins), 2)
        #bitcoin-cli getrawtransaction 8daadcbf5bac325f9b8d7812d82fcca4dd5a594c9f3c7e80727c565ef87e7b73 1|jq ".vin[0].txid"
        self.assertEqual(tx.tx_ins[0].prev_tx, unhexlify('12b86edaa71e73a87618770015b063cf0c4ba493d4ae44c623550a10c352d643'))
        #bitcoin-cli getrawtransaction 8daadcbf5bac325f9b8d7812d82fcca4dd5a594c9f3c7e80727c565ef87e7b73 1|jq ".vin[0].vout"
        self.assertEqual(tx.tx_ins[0].prev_index, 3)
        #bitcoin-cli getrawtransaction 8daadcbf5bac325f9b8d7812d82fcca4dd5a594c9f3c7e80727c565ef87e7b73 1|jq .vin[0].scriptSig.asm
        self.assertEqual(tx.tx_ins[0].script_sig.serialize(), b'')
        #bitcoin-cli getrawtransaction 8daadcbf5bac325f9b8d7812d82fcca4dd5a594c9f3c7e80727c565ef87e7b73 1|jq ".vin[0].sequence"
        self.assertEqual(tx.tx_ins[0].sequence, 0xffffffff)

    def test_parse_outputs(self):
        tx = Tx.parse(BytesIO(unhexlify(TxTest.raw_tx_hex)))
        # bitcoin-cli getrawtransaction 8daadcbf5bac325f9b8d7812d82fcca4dd5a594c9f3c7e80727c565ef87e7b73 1|jq ".vout|length"
        self.assertEqual(len(tx.tx_outs), 4)
        # bitcoin-cli getrawtransaction 8daadcbf5bac325f9b8d7812d82fcca4dd5a594c9f3c7e80727c565ef87e7b73 1|jq ".vout[3].value"
        self.assertEqual(tx.tx_outs[3].amount, int(0.865668 * 100e6))
        # bitcoin-cli getrawtransaction 8daadcbf5bac325f9b8d7812d82fcca4dd5a594c9f3c7e80727c565ef87e7b73 1|jq ".vout[3].scriptPubKey.hex"
        self.assertEqual(tx.tx_outs[3].script_pubkey.serialize(), unhexlify('0020701a8d401c84fb13e6baf169d59684e17abd9fa216c8cc5b9fc63d622ff8c58d'))

    def test_parse_witness(self):
        tx = Tx.parse(BytesIO(unhexlify(TxTest.raw_tx_hex)))
        # bitcoin-cli getrawtransaction 8daadcbf5bac325f9b8d7812d82fcca4dd5a594c9f3c7e80727c565ef87e7b73 1|jq "[.vin[]|.txinwitness]"
        want = [["",
            "30450221009d9ab1b126e25631c20c444f46c600d794c4aa8b4c864a3e664967f4c6ecc73b022019201da2fa3dc67d37817fbb3b91d160993892cbb1a0b335a8f61889871f39f101",
            "304402201f6692696d35c615df6273daf60600f4b1b3f2e7d52610f791bcdc551eaf581a022001869225fc6069e9c37cdf3ad702a4d87c4ffa1a6b779f468cd15f78729a4e6101",
            "52210266edd4ef2953675faf0662c088a7f620935807d200d65387290b31648e51e253210372ce38027ee95c98cdc54172964fa3aecf9f24b85c139d3d203365d6b691d0502103c96d495bfdd5ba4145e3e046fee45e84a8a48ad05bd8dbb395c011a32cf9f88053ae"],
              ["",
               "3045022100fc8f367e892acc7a85b26e404005160d8bf0e3dcd87ad0e1dc40744780b5d42602206ae3e6fed3fbc1bd57631bd51c079f1a054f6e44bc9b84be37fa231ff3c0808401",
               "3044022007221455aae1bb958c3d08e51cd5aeddf348336c13893176c94ee3289262e570022009119f5878a310fdc530432bd87f47b700cabc6c8b8ac9e5d19ac5795472ef0801",
               "52210375e00eb72e29da82b89367947f29ef34afb75e8654f6ea368e0acdfd92976b7c2103a1b26313f430c4b15bb1fdce663207659d8cac749a0e53d70eff01874496feff2103c96d495bfdd5ba4145e3e046fee45e84a8a48ad05bd8dbb395c011a32cf9f88053ae"
        ]]

        for ix in range(len(tx.tx_ins)):
            for iy in range(len(tx.witnesses[ix])):
                self.assertEqual(tx.witnesses[ix][iy], unhexlify(want[ix][iy]))

    def test_parse_locktime(self):
        tx = Tx.parse(BytesIO(unhexlify(TxTest.raw_tx_hex)))
        # bitcoin-cli getrawtransaction 8daadcbf5bac325f9b8d7812d82fcca4dd5a594c9f3c7e80727c565ef87e7b73 1|jq .locktime
        self.assertEqual(tx.locktime, 0)

    def test_serialize(self):
        tx = Tx.parse(BytesIO(unhexlify(TxTest.raw_tx_hex)))
        print("txid=",hexlify(tx.txid()))
        print("hash=",hexlify(tx.hash()))
        self.assertEqual(tx.serialize(), unhexlify(TxTest.raw_tx_hex))