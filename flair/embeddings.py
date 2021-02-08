import os
import re
import logging
from abc import abstractmethod
from collections import Counter
from pathlib import Path
from typing import List, Union, Dict
import inspect

import gensim
import numpy as np
import torch
from bpemb import BPEmb
from deprecated import deprecated
from torch.nn import ParameterList, Parameter
import pdb
from pytorch_transformers import (
    BertTokenizer,
    BertModel,
    RobertaTokenizer,
    RobertaModel,
    TransfoXLTokenizer,
    TransfoXLModel,
    OpenAIGPTModel,
    OpenAIGPTTokenizer,
    GPT2Model,
    GPT2Tokenizer,
    XLNetTokenizer,
    XLMTokenizer,
    XLNetModel,
    XLMModel,
    PreTrainedTokenizer,
    PreTrainedModel,
)

from transformers import (
    XLMRobertaModel,
    XLMRobertaTokenizer,
    )

from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence

import flair
from flair.data import Corpus
from .nn import LockedDropout, WordDropout
from .data import Dictionary, Token, Sentence
from .file_utils import cached_path, open_inside_zip, instance_lru_cache

log = logging.getLogger("flair")


def rand_emb(embedding):
    bias = np.sqrt(3.0 / embedding.size(0))
    torch.nn.init.uniform_(embedding, -bias, bias)
    return embedding

class Embeddings(torch.nn.Module):
    """Abstract base class for all embeddings. Every new type of embedding must implement these methods."""

    def __init__(self):
        """Set some attributes that would otherwise result in errors. Overwrite these in your embedding class."""
        if not hasattr(self, "name"):
            self.name: str = "unnamed_embedding"
        if not hasattr(self, "static_embeddings"):
            # if the embeddings for a sentence are the same in each epoch, set this to True for improved efficiency
            self.static_embeddings = False
        super().__init__()

    @property
    @abstractmethod
    def embedding_length(self) -> int:
        """Returns the length of the embedding vector."""
        pass

    @property
    @abstractmethod
    def embedding_type(self) -> str:
        pass

    def embed(self, sentences: Union[Sentence, List[Sentence]]) -> List[Sentence]:
        """Add embeddings to all words in a list of sentences. If embeddings are already added, updates only if embeddings
        are non-static."""

        # if only one sentence is passed, convert to list of sentence
        if (type(sentences) is Sentence) or (type(sentences) is Image):
            sentences = [sentences]

        everything_embedded: bool = True

        if self.embedding_type == "word-level":
            for sentence in sentences:
                for token in sentence.tokens:
                    if self.name not in token._embeddings.keys():
                        everything_embedded = False
                        break
        else:
            for sentence in sentences:
                if self.name not in sentence._embeddings.keys():
                    everything_embedded = False
                    break

        if not everything_embedded or not self.static_embeddings:
            self._add_embeddings_internal(sentences)

        return sentences

    @abstractmethod
    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:
        """Private method for adding embeddings to all words in a list of sentences."""
        pass

    def get_names(self) -> List[str]:
        """Returns a list of embedding names. In most cases, it is just a list with one item, namely the name of
        this embedding. But in some cases, the embedding is made up by different embeddings (StackedEmbedding).
        Then, the list contains the names of all embeddings in the stack."""
        return [self.name]

    def get_named_embeddings_dict(self) -> Dict:
        return {self.name: self}

    def assign_batch_features(self, sentences, embedding_length=None):
        if embedding_length is None:
            embedding_length = self.embedding_length
        sentence_lengths = [len(x) for x in sentences]
        sentence_tensor = torch.zeros([len(sentences),max(sentence_lengths),embedding_length]).type_as(sentences[0][0]._embeddings[self.name])
        for sent_id, sentence in enumerate(sentences):
            for token_id, token in enumerate(sentence):
                sentence_tensor[sent_id,token_id]=token._embeddings[self.name]
        sentences.features[self.name]=sentence_tensor.cpu()
        return sentences


class TokenEmbeddings(Embeddings):
    """Abstract base class for all token-level embeddings. Ever new type of word embedding must implement these methods."""

    @property
    @abstractmethod
    def embedding_length(self) -> int:
        """Returns the length of the embedding vector."""
        pass

    @property
    def embedding_type(self) -> str:
        return "word-level"

    @staticmethod
    def get_instance_parameters(locals: dict) -> dict:
        class_definition = locals.get("__class__")
        instance_parameters = set(inspect.getfullargspec(class_definition.__init__).args)
        instance_parameters.difference_update(set(["self"]))
        instance_parameters.update(set(["__class__"]))
        instance_parameters = {class_attribute: attribute_value for class_attribute, attribute_value in locals.items()
                               if class_attribute in instance_parameters}
        return instance_parameters

class DocumentEmbeddings(Embeddings):
    """Abstract base class for all document-level embeddings. Ever new type of document embedding must implement these methods."""

    @property
    @abstractmethod
    def embedding_length(self) -> int:
        """Returns the length of the embedding vector."""
        pass

    @property
    def embedding_type(self) -> str:
        return "sentence-level"


class StackedEmbeddings(TokenEmbeddings):
    """A stack of embeddings, used if you need to combine several different embedding types."""

    def __init__(self, embeddings: List[TokenEmbeddings]):
        """The constructor takes a list of embeddings to be combined."""
        super().__init__()

        self.embeddings = embeddings

        # IMPORTANT: add embeddings as torch modules
        for i, embedding in enumerate(embeddings):
            self.add_module("list_embedding_{}".format(i), embedding)

        self.name: str = "Stack"
        self.static_embeddings: bool = True

        self.__embedding_type: str = embeddings[0].embedding_type

        self.__embedding_length: int = 0
        for embedding in embeddings:
            self.__embedding_length += embedding.embedding_length

    def embed(
        self, sentences: Union[Sentence, List[Sentence]], static_embeddings: bool = True
    ):
        # if only one sentence is passed, convert to list of sentence
        if type(sentences) is Sentence:
            sentences = [sentences]

        for embedding in self.embeddings:
            embedding.embed(sentences)

    @property
    def embedding_type(self) -> str:
        return self.__embedding_type

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:
        for embedding in self.embeddings:
            embedding._add_embeddings_internal(sentences)

        return sentences

    def __str__(self):
        return f'StackedEmbeddings [{",".join([str(e) for e in self.embeddings])}]'

class WordEmbeddings(TokenEmbeddings):
    """Standard static word embeddings, such as GloVe or FastText."""

    def __init__(self, embeddings: str, field: str = None):
        """
        Initializes classic word embeddings. Constructor downloads required files if not there.
        :param embeddings: one of: 'glove', 'extvec', 'crawl' or two-letter language code or custom
        If you want to use a custom embedding file, just pass the path to the embeddings as embeddings variable.
        """
        self.embeddings = embeddings

        self.instance_parameters = self.get_instance_parameters(locals=locals())

        hu_path: str = "https://flair.informatik.hu-berlin.de/resources/embeddings/token"

        cache_dir = Path("embeddings")

        # GLOVE embeddings
        if embeddings.lower() == "glove" or embeddings.lower() == "en-glove":
            cached_path(f"{hu_path}/glove.gensim.vectors.npy", cache_dir=cache_dir)
            embeddings = cached_path(f"{hu_path}/glove.gensim", cache_dir=cache_dir)

        # TURIAN embeddings
        elif embeddings.lower() == "turian" or embeddings.lower() == "en-turian":
            cached_path(f"{hu_path}/turian.vectors.npy", cache_dir=cache_dir)
            embeddings = cached_path(f"{hu_path}/turian", cache_dir=cache_dir)

        # KOMNINOS embeddings
        elif embeddings.lower() == "extvec" or embeddings.lower() == "en-extvec":
            cached_path(f"{hu_path}/extvec.gensim.vectors.npy", cache_dir=cache_dir)
            embeddings = cached_path(f"{hu_path}/extvec.gensim", cache_dir=cache_dir)

        # pubmed embeddings
        elif embeddings.lower() == "pubmed" or embeddings.lower() == "en-pubmed":
            cached_path(f"{hu_path}/pubmed_pmc_wiki_sg_1M.gensim.vectors.npy", cache_dir=cache_dir)
            embeddings = cached_path(f"{hu_path}/pubmed_pmc_wiki_sg_1M.gensim", cache_dir=cache_dir)

        # FT-CRAWL embeddings
        elif embeddings.lower() == "crawl" or embeddings.lower() == "en-crawl":
            cached_path(f"{hu_path}/en-fasttext-crawl-300d-1M.vectors.npy", cache_dir=cache_dir)
            embeddings = cached_path(f"{hu_path}/en-fasttext-crawl-300d-1M", cache_dir=cache_dir)

        # FT-CRAWL embeddings
        elif embeddings.lower() in ["news", "en-news", "en"]:
            cached_path(f"{hu_path}/en-fasttext-news-300d-1M.vectors.npy", cache_dir=cache_dir)
            embeddings = cached_path(f"{hu_path}/en-fasttext-news-300d-1M", cache_dir=cache_dir)

        # twitter embeddings
        elif embeddings.lower() in ["twitter", "en-twitter"]:
            cached_path(f"{hu_path}/twitter.gensim.vectors.npy", cache_dir=cache_dir)
            embeddings = cached_path(f"{hu_path}/twitter.gensim", cache_dir=cache_dir)

        # two-letter language code wiki embeddings
        elif len(embeddings.lower()) == 2:
            cached_path(f"{hu_path}/{embeddings}-wiki-fasttext-300d-1M.vectors.npy", cache_dir=cache_dir)
            embeddings = cached_path(f"{hu_path}/{embeddings}-wiki-fasttext-300d-1M", cache_dir=cache_dir)

        # two-letter language code wiki embeddings
        elif len(embeddings.lower()) == 7 and embeddings.endswith("-wiki"):
            cached_path(f"{hu_path}/{embeddings[:2]}-wiki-fasttext-300d-1M.vectors.npy", cache_dir=cache_dir)
            embeddings = cached_path(f"{hu_path}/{embeddings[:2]}-wiki-fasttext-300d-1M", cache_dir=cache_dir)

        # two-letter language code crawl embeddings
        elif len(embeddings.lower()) == 8 and embeddings.endswith("-crawl"):
            cached_path(f"{hu_path}/{embeddings[:2]}-crawl-fasttext-300d-1M.vectors.npy", cache_dir=cache_dir)
            embeddings = cached_path(f"{hu_path}/{embeddings[:2]}-crawl-fasttext-300d-1M", cache_dir=cache_dir)

        elif not Path(embeddings).exists():
            raise ValueError(
                f'The given embeddings "{embeddings}" is not available or is not a valid path.'
            )

        self.name: str = str(embeddings)
        self.static_embeddings = True

        if str(embeddings).endswith(".bin"):
            self.precomputed_word_embeddings = gensim.models.KeyedVectors.load_word2vec_format(
                str(embeddings), binary=True
            )
        else:
            self.precomputed_word_embeddings = gensim.models.KeyedVectors.load(
                str(embeddings)
            )

        self.field = field

        self.__embedding_length: int = self.precomputed_word_embeddings.vector_size
        super().__init__()

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    @instance_lru_cache(maxsize=10000, typed=False)
    def get_cached_vec(self, word: str) -> torch.Tensor:
        if word in self.precomputed_word_embeddings:
            word_embedding = self.precomputed_word_embeddings[word]
        elif word.lower() in self.precomputed_word_embeddings:
            word_embedding = self.precomputed_word_embeddings[word.lower()]
        elif re.sub(r"\d", "#", word.lower()) in self.precomputed_word_embeddings:
            word_embedding = self.precomputed_word_embeddings[
                re.sub(r"\d", "#", word.lower())
            ]
        elif re.sub(r"\d", "0", word.lower()) in self.precomputed_word_embeddings:
            word_embedding = self.precomputed_word_embeddings[
                re.sub(r"\d", "0", word.lower())
            ]
        else:
            word_embedding = np.zeros(self.embedding_length, dtype="float")

        word_embedding = torch.tensor(
            word_embedding.tolist(), device=flair.device, dtype=torch.float
        )
        return word_embedding

    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:

        for i, sentence in enumerate(sentences):

            for token, token_idx in zip(sentence.tokens, range(len(sentence.tokens))):

                if "field" not in self.__dict__ or self.field is None:
                    word = token.text
                else:
                    word = token.get_tag(self.field).value

                word_embedding = self.get_cached_vec(word=word)

                token.set_embedding(self.name, word_embedding)

        return sentences

    def __str__(self):
        return self.name

    def extra_repr(self):
        # fix serialized models
        if "embeddings" not in self.__dict__:
            self.embeddings = self.name

        return f"'{self.embeddings}'"

class FastWordEmbeddings(TokenEmbeddings):
    """Standard Fine Tune word embeddings, such as GloVe or FastText."""

    def __init__(self, embeddings: str, all_tokens: list, field: str = None, if_cased: bool = True, freeze: bool = False, additional_empty_embedding: bool = False, keepall: bool = False,):
        """
        Initializes classic word embeddings. Constructor downloads required files if not there.
        :param embeddings: one of: 'glove', 'extvec', 'crawl' or two-letter language code or custom
        If you want to use a custom embedding file, just pass the path to the embeddings as embeddings variable.
        """
        super().__init__()
        embed_name = embeddings
        old_base_path = (
            "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings/"
        )
        base_path = (
            "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings-v0.3/"
        )
        embeddings_path_v4 = (
            "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings-v0.4/"
        )
        embeddings_path_v4_1 = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings-v0.4.1/"

        cache_dir = Path("embeddings")

        # GLOVE embeddings
        if embeddings.lower() == "glove" or embeddings.lower() == "en-glove":
            cached_path(f"{old_base_path}glove.gensim.vectors.npy", cache_dir=cache_dir)
            embeddings = cached_path(
                f"{old_base_path}glove.gensim", cache_dir=cache_dir
            )

        # TURIAN embeddings
        elif embeddings.lower() == "turian" or embeddings.lower() == "en-turian":
            cached_path(
                f"{embeddings_path_v4_1}turian.vectors.npy", cache_dir=cache_dir
            )
            embeddings = cached_path(
                f"{embeddings_path_v4_1}turian", cache_dir=cache_dir
            )

        # KOMNINOS embeddings
        elif embeddings.lower() == "extvec" or embeddings.lower() == "en-extvec":
            cached_path(
                f"{old_base_path}extvec.gensim.vectors.npy", cache_dir=cache_dir
            )
            embeddings = cached_path(
                f"{old_base_path}extvec.gensim", cache_dir=cache_dir
            )

        # FT-CRAWL embeddings
        elif embeddings.lower() == "crawl" or embeddings.lower() == "en-crawl":
            cached_path(
                f"{base_path}en-fasttext-crawl-300d-1M.vectors.npy", cache_dir=cache_dir
            )
            embeddings = cached_path(
                f"{base_path}en-fasttext-crawl-300d-1M", cache_dir=cache_dir
            )

        # FT-CRAWL embeddings
        elif (
            embeddings.lower() == "news"
            or embeddings.lower() == "en-news"
            or embeddings.lower() == "en"
        ):
            cached_path(
                f"{base_path}en-fasttext-news-300d-1M.vectors.npy", cache_dir=cache_dir
            )
            embeddings = cached_path(
                f"{base_path}en-fasttext-news-300d-1M", cache_dir=cache_dir
            )

        # twitter embeddings
        elif embeddings.lower() == "twitter" or embeddings.lower() == "en-twitter":
            cached_path(
                f"{old_base_path}twitter.gensim.vectors.npy", cache_dir=cache_dir
            )
            embeddings = cached_path(
                f"{old_base_path}twitter.gensim", cache_dir=cache_dir
            )

        # two-letter language code wiki embeddings
        elif len(embeddings.lower()) == 2:
            cached_path(
                f"{embeddings_path_v4}{embeddings}-wiki-fasttext-300d-1M.vectors.npy",
                cache_dir=cache_dir,
            )
            embeddings = cached_path(
                f"{embeddings_path_v4}{embeddings}-wiki-fasttext-300d-1M",
                cache_dir=cache_dir,
            )

        # two-letter language code wiki embeddings
        elif len(embeddings.lower()) == 7 and embeddings.endswith("-wiki"):
            cached_path(
                f"{embeddings_path_v4}{embeddings[:2]}-wiki-fasttext-300d-1M.vectors.npy",
                cache_dir=cache_dir,
            )
            embeddings = cached_path(
                f"{embeddings_path_v4}{embeddings[:2]}-wiki-fasttext-300d-1M",
                cache_dir=cache_dir,
            )

        # two-letter language code crawl embeddings
        elif len(embeddings.lower()) == 8 and embeddings.endswith("-crawl"):
            cached_path(
                f"{embeddings_path_v4}{embeddings[:2]}-crawl-fasttext-300d-1M.vectors.npy",
                cache_dir=cache_dir,
            )
            embeddings = cached_path(
                f"{embeddings_path_v4}{embeddings[:2]}-crawl-fasttext-300d-1M",
                cache_dir=cache_dir,
            )
        elif embeddings.lower().startswith('conll_'):
            embeddings = Path(flair.cache_root) / cache_dir / f'{embeddings.lower()}.txt'
        elif embeddings.lower().endswith('txt'):
            embeddings = Path(flair.cache_root) / cache_dir / f'{embeddings}'
        elif embeddings.lower().endswith('.cc'):
            embeddings = Path(flair.cache_root) / cache_dir / f'{embeddings}'
        elif embeddings.lower().endswith('.vec'):
            embeddings = Path(flair.cache_root) / cache_dir / f'{embeddings}'
        elif embeddings.lower().endswith('cc.el'):
            embeddings = Path(flair.cache_root) / cache_dir / f'{embeddings.lower()}.300.txt'
        elif not Path(embeddings).exists():
            raise ValueError(
                f'The given embeddings "{embeddings}" is not available or is not a valid path.'
            )

        self.static_embeddings = False
        if str(embeddings).endswith(".bin"):
            precomputed_word_embeddings = gensim.models.KeyedVectors.load_word2vec_format(
                str(embeddings), binary=True
            )
        elif str(embeddings).endswith('.txt'):
            precomputed_word_embeddings = gensim.models.KeyedVectors.load_word2vec_format(
                str(embeddings), binary=False
            )
        elif str(embeddings).endswith('.vec'):
            precomputed_word_embeddings = gensim.models.KeyedVectors.load_word2vec_format(str(embeddings), binary=False)
        else:
            precomputed_word_embeddings = gensim.models.KeyedVectors.load(
                str(embeddings)
            )
        self.field = field

        self.name = f'Word: {embed_name}'
        self.__embedding_length: int = precomputed_word_embeddings.vector_size
        self.if_cased = if_cased
        self.get = self.get_idx_cased if if_cased else self.get_idx

        train_set = set([token for token in all_tokens[0]])  # | set([token.lower() for token in all_tokens[0]])
        full_set = set([token for token in all_tokens[1]])  # | set([token.lower() for token in all_tokens[1]])

        self.vocab = {}
        self.vocab['unk'] = len(self.vocab)

        if 'unk' in precomputed_word_embeddings:
            embeddings_tmp = [torch.FloatTensor(precomputed_word_embeddings['unk']).unsqueeze(0)]
        else:
            embeddings_tmp = [rand_emb(
                torch.FloatTensor(self.__embedding_length)
            ).unsqueeze(0)]

        in_train = True
        train_emb = 0
        train_rand = 0
        if keepall:
            full_set=set(precomputed_word_embeddings.vocab.keys())|full_set
        for token in full_set:
            if token in precomputed_word_embeddings:
                word_embedding = torch.FloatTensor(precomputed_word_embeddings[token])
                train_emb += 1
            elif token.lower() in precomputed_word_embeddings:
                word_embedding = torch.FloatTensor(precomputed_word_embeddings[token.lower()])
                train_emb += 1
            elif re.sub(r"\d", "#", token.lower()) in precomputed_word_embeddings:
                word_embedding = torch.FloatTensor(
                    precomputed_word_embeddings[re.sub(r"\d", "#", token.lower())]
                )
            elif re.sub(r"\d", "0", token.lower()) in precomputed_word_embeddings:
                word_embedding = torch.FloatTensor(
                    precomputed_word_embeddings[re.sub(r"\d", "0", token.lower())]
                )
            else:
                if token in train_set:
                    word_embedding = rand_emb(
                        torch.FloatTensor(self.__embedding_length)
                    )
                else:
                    in_train = False
                    pass

                # word_embedding = word_embedding.view(word_embedding.size()[0]*word_embedding.size()[1])
            if in_train:
                if token not in self.vocab:
                    embeddings_tmp.append(word_embedding.unsqueeze(0))
                    self.vocab[token] = len(self.vocab)
                    train_rand += 1
            else:
                in_train = True
        for i in range(3):
            embeddings_tmp.append(rand_emb(
                            torch.FloatTensor(self.__embedding_length)
                        ).unsqueeze(0))
        self.vocab['<start>'] = len(self.vocab)
        self.vocab['<end>'] = len(self.vocab)
        self.vocab['<eof>'] = len(self.vocab)
        embeddings_tmp = torch.cat(embeddings_tmp, 0)
        assert len(self.vocab) == embeddings_tmp.size()[0], "vocab_dic and embedding size not match!"
        self.word_embedding = torch.nn.Embedding(embeddings_tmp.shape[0], embeddings_tmp.shape[1])
        self.word_embedding.weight = torch.nn.Parameter(embeddings_tmp)
        if freeze:
            
            self.word_embedding.weight.requires_grad=False
        self.additional_empty_embedding=additional_empty_embedding
        if self.additional_empty_embedding:
            self.empty_embedding = torch.nn.Embedding(embeddings_tmp.shape[0], embeddings_tmp.shape[1])
            torch.nn.init.zeros_(self.empty_embedding.weight)
            
            # self.empty_embedding.weight = rand_emb(self.empty_embedding.weight)
    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def get_idx(self, token: str):
        return self.vocab.get(token.lower(), self.vocab['unk'])

    def get_idx_cased(self, token: str):
        return self.vocab.get(token, self.vocab.get(token.lower(), self.vocab['unk']))

    def encode_sentences(self, sentence):
        tokens = sentence.tokens
        lines = torch.LongTensor(list(map(self.get, tokens)))
        embed = sentence.set_word(lines.unsqueeze(0))
        return embed

    def embed_sentences(self, sentences):
        words=sentences.words.to(flair.device)
        embeddings = self.word_embedding(words)
        if self.additional_empty_embedding:
            embeddings +=self.empty_embedding(words)
        return embeddings

    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:
        embeddings = self.embed_sentences(sentences)
        sentences.features[self.name]=embeddings.cpu()
        return sentences

    def __str__(self):
        return self.name

class FastCharacterEmbeddings(TokenEmbeddings):
    """Character embeddings of words, as proposed in Lample et al., 2016."""

    def __init__(
        self,
        vocab: List = None,
        char_embedding_dim: int = 25,
        hidden_size_char: int = 25,
        char_cnn = False,
        debug = False,
    ):
        """Uses the default character dictionary if none provided."""

        super().__init__()
        self.name = "Char"
        self.static_embeddings = False
        self.char_cnn = char_cnn
        self.debug = debug
        # use list of common characters if none provided
        self.char_dictionary = {'<u>': 0}
        for word in vocab:
            for c in word:
                if c not in self.char_dictionary:
                    self.char_dictionary[c] = len(self.char_dictionary)

        self.char_dictionary[' '] = len(self.char_dictionary)  # concat for char
        self.char_dictionary['\n'] = len(self.char_dictionary)  # eof for char

        self.char_embedding_dim: int = char_embedding_dim
        self.hidden_size_char: int = hidden_size_char
        self.char_embedding = torch.nn.Embedding(
            len(self.char_dictionary), self.char_embedding_dim
        )
        if self.char_cnn:
            print("Use character level CNN")
            self.char_drop = torch.nn.Dropout(0.5)
            self.char_layer = torch.nn.Conv1d(char_embedding_dim, hidden_size_char, kernel_size=3, padding=1)
            self.__embedding_length = self.char_embedding_dim
        else:
            self.char_layer = torch.nn.LSTM(
                self.char_embedding_dim,
                self.hidden_size_char,
                num_layers=1,
                bidirectional=True,
            )

            self.__embedding_length = self.char_embedding_dim * 2
        self.pad_word = rand_emb(torch.FloatTensor(self.__embedding_length)).unsqueeze(0).unsqueeze(0)

    def _init_embeddings(self):
        utils.init_embedding(self.char_embedding)

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def embed_sentences(self, sentences):
        # batch_size = len(sentences)
        # char_batch = sentences[0].total_len
        batch_size = len(sentences)
        char_batch = sentences.max_sent_len
        # char_list = []
        # char_lens = []
        # for sent in sentences:
        #     char_list.append(sent.char_id)
        #     char_lens.append(sent.char_lengths)
        # char_lengths = torch.cat(char_lens, 0)
        # char_seqs = pad_sequence(char_list, batch_first=False, padding_value=0)
        # char_seqs = char_seqs.view(-1, batch_size * char_batch)
        char_seqs = sentences.char_seqs.to(flair.device)
        char_lengths = sentences.char_lengths.to(flair.device)
        char_embeds = self.char_embedding(char_seqs)
        if self.char_cnn:
            char_embeds = self.char_drop(char_embeds)
            char_cnn_out = self.char_layer(char_embeds.permute(1,2,0))
            char_cnn_out = torch.nn.functional.max_pool1d(char_cnn_out, char_cnn_out.size(2)).view(batch_size, char_batch, -1)
            outs = char_cnn_out
        else:
            pack_char_seqs = pack_padded_sequence(input=char_embeds, lengths=char_lengths, batch_first=False, enforce_sorted=False)
            lstm_out, hidden = self.char_layer(pack_char_seqs, None)
            # lstm_out = lstm_out.view(-1, self.hidden_dim)
            # hidden[0] = h_t = (2, b * s, 25)
            outs = hidden[0].transpose(0, 1).contiguous().view(batch_size * char_batch, -1)
            outs = outs.view(batch_size, char_batch, -1)

        return outs
    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:
        embeddings = self.embed_sentences(sentences)
        sentences.features[self.name]=embeddings.cpu()
        return sentences

    def __str__(self):
        return self.name




class FastTextEmbeddings(TokenEmbeddings):
    """FastText Embeddings with oov functionality"""

    def __init__(self, embeddings: str, use_local: bool = True, field: str = None):
        """
        Initializes fasttext word embeddings. Constructor downloads required embedding file and stores in cache
        if use_local is False.

        :param embeddings: path to your embeddings '.bin' file
        :param use_local: set this to False if you are using embeddings from a remote source
        """

        cache_dir = Path("embeddings")

        if use_local:
            if not Path(embeddings).exists():
                raise ValueError(
                    f'The given embeddings "{embeddings}" is not available or is not a valid path.'
                )
        else:
            embeddings = cached_path(f"{embeddings}", cache_dir=cache_dir)

        self.embeddings = embeddings

        self.name: str = str(embeddings)

        self.static_embeddings = True

        self.precomputed_word_embeddings = gensim.models.FastText.load_fasttext_format(
            str(embeddings)
        )

        self.__embedding_length: int = self.precomputed_word_embeddings.vector_size

        self.field = field
        super().__init__()

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:

        for i, sentence in enumerate(sentences):

            for token, token_idx in zip(sentence.tokens, range(len(sentence.tokens))):

                if "field" not in self.__dict__ or self.field is None:
                    word = token.text
                else:
                    word = token.get_tag(self.field).value

                try:
                    word_embedding = self.precomputed_word_embeddings[word]
                except:
                    word_embedding = np.zeros(self.embedding_length, dtype="float")

                word_embedding = torch.FloatTensor(word_embedding)

                token.set_embedding(self.name, word_embedding)

        return sentences

    def __str__(self):
        return self.name

    def extra_repr(self):
        return f"'{self.embeddings}'"


class OneHotEmbeddings(TokenEmbeddings):
    """One-hot encoded embeddings."""

    def __init__(
        self,
        corpus=Union[Corpus, List[Sentence]],
        field: str = "text",
        embedding_length: int = 300,
        min_freq: int = 3,
    ):

        super().__init__()
        self.name = "one-hot"
        self.static_embeddings = False
        self.min_freq = min_freq

        tokens = list(map((lambda s: s.tokens), corpus.train))
        tokens = [token for sublist in tokens for token in sublist]

        if field == "text":
            most_common = Counter(list(map((lambda t: t.text), tokens))).most_common()
        else:
            most_common = Counter(
                list(map((lambda t: t.get_tag(field)), tokens))
            ).most_common()

        tokens = []
        for token, freq in most_common:
            if freq < min_freq:
                break
            tokens.append(token)

        self.vocab_dictionary: Dictionary = Dictionary()
        for token in tokens:
            self.vocab_dictionary.add_item(token)

        # max_tokens = 500
        self.__embedding_length = embedding_length

        print(self.vocab_dictionary.idx2item)
        print(f"vocabulary size of {len(self.vocab_dictionary)}")

        # model architecture
        self.embedding_layer = torch.nn.Embedding(
            len(self.vocab_dictionary), self.__embedding_length
        )
        torch.nn.init.xavier_uniform_(self.embedding_layer.weight)

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:

        one_hot_sentences = []
        for i, sentence in enumerate(sentences):
            context_idxs = [
                self.vocab_dictionary.get_idx_for_item(t.text) for t in sentence.tokens
            ]

            one_hot_sentences.extend(context_idxs)

        one_hot_sentences = torch.tensor(one_hot_sentences, dtype=torch.long).to(
            flair.device
        )

        embedded = self.embedding_layer.forward(one_hot_sentences)

        index = 0
        for sentence in sentences:
            for token in sentence:
                embedding = embedded[index]
                token.set_embedding(self.name, embedding)
                index += 1

        return sentences

    def __str__(self):
        return self.name

    def extra_repr(self):
        return "min_freq={}".format(self.min_freq)


class BPEmbSerializable(BPEmb):
    def __getstate__(self):
        state = self.__dict__.copy()
        # save the sentence piece model as binary file (not as path which may change)
        state["spm_model_binary"] = open(self.model_file, mode="rb").read()
        state["spm"] = None
        return state

    def __setstate__(self, state):
        from bpemb.util import sentencepiece_load

        model_file = self.model_tpl.format(lang=state["lang"], vs=state["vs"])
        self.__dict__ = state

        # write out the binary sentence piece model into the expected directory
        self.cache_dir: Path = Path(flair.cache_root) / "embeddings"
        if "spm_model_binary" in self.__dict__:
            # if the model was saved as binary and it is not found on disk, write to appropriate path
            if not os.path.exists(self.cache_dir / state["lang"]):
                os.makedirs(self.cache_dir / state["lang"])
            self.model_file = self.cache_dir / model_file
            with open(self.model_file, "wb") as out:
                out.write(self.__dict__["spm_model_binary"])
        else:
            # otherwise, use normal process and potentially trigger another download
            self.model_file = self._load_file(model_file)

        # once the modes if there, load it with sentence piece
        state["spm"] = sentencepiece_load(self.model_file)


class MuseCrosslingualEmbeddings(TokenEmbeddings):
    def __init__(self,):
        self.name: str = f"muse-crosslingual"
        self.static_embeddings = True
        self.__embedding_length: int = 300
        self.language_embeddings = {}
        super().__init__()

    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:

        for i, sentence in enumerate(sentences):

            language_code = sentence.get_language_code()
            print(language_code)
            supported = [
                "en",
                "de",
                "bg",
                "ca",
                "hr",
                "cs",
                "da",
                "nl",
                "et",
                "fi",
                "fr",
                "el",
                "he",
                "hu",
                "id",
                "it",
                "mk",
                "no",
                "pl",
                "pt",
                "ro",
                "ru",
                "sk",
            ]
            if language_code not in supported:
                language_code = "en"

            if language_code not in self.language_embeddings:
                log.info(f"Loading up MUSE embeddings for '{language_code}'!")
                # download if necessary
                webpath = "https://alan-nlp.s3.eu-central-1.amazonaws.com/resources/embeddings-muse"
                cache_dir = Path("embeddings") / "MUSE"
                cached_path(
                    f"{webpath}/muse.{language_code}.vec.gensim.vectors.npy",
                    cache_dir=cache_dir,
                )
                embeddings_file = cached_path(
                    f"{webpath}/muse.{language_code}.vec.gensim", cache_dir=cache_dir
                )

                # load the model
                self.language_embeddings[
                    language_code
                ] = gensim.models.KeyedVectors.load(str(embeddings_file))

            current_embedding_model = self.language_embeddings[language_code]

            for token, token_idx in zip(sentence.tokens, range(len(sentence.tokens))):

                if "field" not in self.__dict__ or self.field is None:
                    word = token.text
                else:
                    word = token.get_tag(self.field).value

                if word in current_embedding_model:
                    word_embedding = current_embedding_model[word]
                elif word.lower() in current_embedding_model:
                    word_embedding = current_embedding_model[word.lower()]
                elif re.sub(r"\d", "#", word.lower()) in current_embedding_model:
                    word_embedding = current_embedding_model[
                        re.sub(r"\d", "#", word.lower())
                    ]
                elif re.sub(r"\d", "0", word.lower()) in current_embedding_model:
                    word_embedding = current_embedding_model[
                        re.sub(r"\d", "0", word.lower())
                    ]
                else:
                    word_embedding = np.zeros(self.embedding_length, dtype="float")

                word_embedding = torch.FloatTensor(word_embedding)

                token.set_embedding(self.name, word_embedding)

        return sentences

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def __str__(self):
        return self.name


class BytePairEmbeddings(TokenEmbeddings):
    def __init__(
        self,
        language: str,
        dim: int = 50,
        syllables: int = 100000,
        cache_dir=Path(flair.cache_root) / "embeddings",
    ):
        """
        Initializes BP embeddings. Constructor downloads required files if not there.
        """

        self.name: str = f"bpe-{language}-{syllables}-{dim}"
        self.static_embeddings = True
        self.embedder = BPEmbSerializable(
            lang=language, vs=syllables, dim=dim, cache_dir=cache_dir
        )

        self.__embedding_length: int = self.embedder.emb.vector_size * 2
        super().__init__()

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:

        for i, sentence in enumerate(sentences):

            for token, token_idx in zip(sentence.tokens, range(len(sentence.tokens))):

                if "field" not in self.__dict__ or self.field is None:
                    word = token.text
                else:
                    word = token.get_tag(self.field).value

                if word.strip() == "":
                    # empty words get no embedding
                    token.set_embedding(
                        self.name, torch.zeros(self.embedding_length, dtype=torch.float)
                    )
                else:
                    # all other words get embedded
                    embeddings = self.embedder.embed(word.lower())
                    embedding = np.concatenate(
                        (embeddings[0], embeddings[len(embeddings) - 1])
                    )
                    token.set_embedding(
                        self.name, torch.tensor(embedding, dtype=torch.float)
                    )

        return sentences

    def __str__(self):
        return self.name

    def extra_repr(self):
        return "model={}".format(self.name)


class ELMoEmbeddings(TokenEmbeddings):
    """Contextual word embeddings using word-level LM, as proposed in Peters et al., 2018."""

    def __init__(
        self, model: str = "original", options_file: str = None, weight_file: str = None
    ):
        super().__init__()

        try:
            import allennlp.commands.elmo
        except ModuleNotFoundError:
            log.warning("-" * 100)
            log.warning('ATTENTION! The library "allennlp" is not installed!')
            log.warning(
                'To use ELMoEmbeddings, please first install with "pip install allennlp"'
            )
            log.warning("-" * 100)
            pass

        self.name = "elmo-" + model
        self.static_embeddings = True

        if not options_file or not weight_file:
            # the default model for ELMo is the 'original' model, which is very large
            options_file = allennlp.commands.elmo.DEFAULT_OPTIONS_FILE
            weight_file = allennlp.commands.elmo.DEFAULT_WEIGHT_FILE
            # alternatively, a small, medium or portuguese model can be selected by passing the appropriate mode name
            if model == "small":
                options_file = "https://s3-us-west-2.amazonaws.com/allennlp/models/elmo/2x1024_128_2048cnn_1xhighway/elmo_2x1024_128_2048cnn_1xhighway_options.json"
                weight_file = "https://s3-us-west-2.amazonaws.com/allennlp/models/elmo/2x1024_128_2048cnn_1xhighway/elmo_2x1024_128_2048cnn_1xhighway_weights.hdf5"
            if model == "medium":
                options_file = "https://s3-us-west-2.amazonaws.com/allennlp/models/elmo/2x2048_256_2048cnn_1xhighway/elmo_2x2048_256_2048cnn_1xhighway_options.json"
                weight_file = "https://s3-us-west-2.amazonaws.com/allennlp/models/elmo/2x2048_256_2048cnn_1xhighway/elmo_2x2048_256_2048cnn_1xhighway_weights.hdf5"
            if model in ["large", "5.5B"]:
                options_file = "https://s3-us-west-2.amazonaws.com/allennlp/models/elmo/2x4096_512_2048cnn_2xhighway_5.5B/elmo_2x4096_512_2048cnn_2xhighway_5.5B_options.json"
                weight_file = "https://s3-us-west-2.amazonaws.com/allennlp/models/elmo/2x4096_512_2048cnn_2xhighway_5.5B/elmo_2x4096_512_2048cnn_2xhighway_5.5B_weights.hdf5"
            if model == "pt" or model == "portuguese":
                options_file = "https://s3-us-west-2.amazonaws.com/allennlp/models/elmo/contributed/pt/elmo_pt_options.json"
                weight_file = "https://s3-us-west-2.amazonaws.com/allennlp/models/elmo/contributed/pt/elmo_pt_weights.hdf5"
            if model == "pubmed":
                options_file = "https://s3-us-west-2.amazonaws.com/allennlp/models/elmo/contributed/pubmed/elmo_2x4096_512_2048cnn_2xhighway_options.json"
                weight_file = "https://s3-us-west-2.amazonaws.com/allennlp/models/elmo/contributed/pubmed/elmo_2x4096_512_2048cnn_2xhighway_weights_PubMed_only.hdf5"

        # put on Cuda if available
        from flair import device

        if re.fullmatch(r"cuda:[0-9]+", str(device)):
            cuda_device = int(str(device).split(":")[-1])
        elif str(device) == "cpu":
            cuda_device = -1
        else:
            cuda_device = 0

        self.ee = allennlp.commands.elmo.ElmoEmbedder(
            options_file=options_file, weight_file=weight_file, cuda_device=cuda_device
        )

        # embed a dummy sentence to determine embedding_length
        dummy_sentence: Sentence = Sentence()
        dummy_sentence.add_token(Token("hello"))
        embedded_dummy = self.embed(dummy_sentence)
        self.__embedding_length: int = len(
            embedded_dummy[0].get_token(1).get_embedding()
        )

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:

        sentence_words: List[List[str]] = []
        for sentence in sentences:
            sentence_words.append([token.text for token in sentence])

        embeddings = self.ee.embed_batch(sentence_words)

        for i, sentence in enumerate(sentences):

            sentence_embeddings = embeddings[i]

            for token, token_idx in zip(sentence.tokens, range(len(sentence.tokens))):
                word_embedding = torch.cat(
                    [
                        torch.FloatTensor(sentence_embeddings[0, token_idx, :]),
                        torch.FloatTensor(sentence_embeddings[1, token_idx, :]),
                        torch.FloatTensor(sentence_embeddings[2, token_idx, :]),
                    ],
                    0,
                )

                token.set_embedding(self.name, word_embedding)

        return sentences

    def extra_repr(self):
        return "model={}".format(self.name)

    def __str__(self):
        return self.name


class ELMoTransformerEmbeddings(TokenEmbeddings):
    """Contextual word embeddings using word-level Transformer-based LM, as proposed in Peters et al., 2018."""

    @deprecated(
        version="0.4.2",
        reason="Not possible to load or save ELMo Transformer models. @stefan-it is working on it.",
    )
    def __init__(self, model_file: str):
        super().__init__()

        try:
            from allennlp.modules.token_embedders.bidirectional_language_model_token_embedder import (
                BidirectionalLanguageModelTokenEmbedder,
            )
            from allennlp.data.token_indexers.elmo_indexer import (
                ELMoTokenCharactersIndexer,
            )
        except ModuleNotFoundError:
            log.warning("-" * 100)
            log.warning('ATTENTION! The library "allennlp" is not installed!')
            log.warning(
                "To use ELMoTransformerEmbeddings, please first install a recent version from https://github.com/allenai/allennlp"
            )
            log.warning("-" * 100)
            pass

        self.name = "elmo-transformer"
        self.static_embeddings = True
        self.lm_embedder = BidirectionalLanguageModelTokenEmbedder(
            archive_file=model_file,
            dropout=0.2,
            bos_eos_tokens=("<S>", "</S>"),
            remove_bos_eos=True,
            requires_grad=False,
        )
        self.lm_embedder = self.lm_embedder.to(device=flair.device)
        self.vocab = self.lm_embedder._lm.vocab
        self.indexer = ELMoTokenCharactersIndexer()

        # embed a dummy sentence to determine embedding_length
        dummy_sentence: Sentence = Sentence()
        dummy_sentence.add_token(Token("hello"))
        embedded_dummy = self.embed(dummy_sentence)
        self.__embedding_length: int = len(
            embedded_dummy[0].get_token(1).get_embedding()
        )

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:
        # Avoid conflicts with flair's Token class
        import allennlp.data.tokenizers.token as allen_nlp_token

        indexer = self.indexer
        vocab = self.vocab

        for sentence in sentences:
            character_indices = indexer.tokens_to_indices(
                [allen_nlp_token.Token(token.text) for token in sentence], vocab, "elmo"
            )["elmo"]

            indices_tensor = torch.LongTensor([character_indices])
            indices_tensor = indices_tensor.to(device=flair.device)
            embeddings = self.lm_embedder(indices_tensor)[0].detach().cpu().numpy()

            for token, token_idx in zip(sentence.tokens, range(len(sentence.tokens))):
                embedding = embeddings[token_idx]
                word_embedding = torch.FloatTensor(embedding)
                token.set_embedding(self.name, word_embedding)

        return sentences

    def extra_repr(self):
        return "model={}".format(self.name)

    def __str__(self):
        return self.name


class ScalarMix(torch.nn.Module):
    """
    Computes a parameterised scalar mixture of N tensors.
    This method was proposed by Liu et al. (2019) in the paper:
    "Linguistic Knowledge and Transferability of Contextual Representations" (https://arxiv.org/abs/1903.08855)

    The implementation is copied and slightly modified from the allennlp repository and is licensed under Apache 2.0.
    It can be found under:
    https://github.com/allenai/allennlp/blob/master/allennlp/modules/scalar_mix.py.
    """

    def __init__(self, mixture_size: int) -> None:
        """
        Inits scalar mix implementation.
        ``mixture = gamma * sum(s_k * tensor_k)`` where ``s = softmax(w)``, with ``w`` and ``gamma`` scalar parameters.
        :param mixture_size: size of mixtures (usually the number of layers)
        """
        super(ScalarMix, self).__init__()
        self.mixture_size = mixture_size

        initial_scalar_parameters = [0.0] * mixture_size

        self.scalar_parameters = ParameterList(
            [
                Parameter(
                    torch.FloatTensor([initial_scalar_parameters[i]]).to(flair.device),
                    requires_grad=False,
                )
                for i in range(mixture_size)
            ]
        )
        self.gamma = Parameter(
            torch.FloatTensor([1.0]).to(flair.device), requires_grad=False
        )

    def forward(self, tensors: List[torch.Tensor]) -> torch.Tensor:
        """
        Computes a weighted average of the ``tensors``.  The input tensors an be any shape
        with at least two dimensions, but must all be the same shape.
        :param tensors: list of input tensors
        :return: computed weighted average of input tensors
        """
        if len(tensors) != self.mixture_size:
            log.error(
                "{} tensors were passed, but the module was initialized to mix {} tensors.".format(
                    len(tensors), self.mixture_size
                )
            )

        normed_weights = torch.nn.functional.softmax(
            torch.cat([parameter for parameter in self.scalar_parameters]), dim=0
        )
        normed_weights = torch.split(normed_weights, split_size_or_sections=1)

        pieces = []
        for weight, tensor in zip(normed_weights, tensors):
            pieces.append(weight * tensor)
        return self.gamma * sum(pieces)


def _extract_embeddings(
    hidden_states: List[torch.FloatTensor],
    layers: List[int],
    pooling_operation: str,
    subword_start_idx: int,
    subword_end_idx: int,
    use_scalar_mix: bool = False,
) -> List[torch.FloatTensor]:
    """
    Extracts subword embeddings from specified layers from hidden states.
    :param hidden_states: list of hidden states from model
    :param layers: list of layers
    :param pooling_operation: pooling operation for subword embeddings (supported: first, last, first_last and mean)
    :param subword_start_idx: defines start index for subword
    :param subword_end_idx: defines end index for subword
    :param use_scalar_mix: determines, if scalar mix should be used
    :return: list of extracted subword embeddings
    """
    subtoken_embeddings: List[torch.FloatTensor] = []

    for layer in layers:
        current_embeddings = hidden_states[layer][0][subword_start_idx:subword_end_idx]

        first_embedding: torch.FloatTensor = current_embeddings[0]
        if pooling_operation == "first_last":
            last_embedding: torch.FloatTensor = current_embeddings[-1]
            final_embedding: torch.FloatTensor = torch.cat(
                [first_embedding, last_embedding]
            )
        elif pooling_operation == "last":
            final_embedding: torch.FloatTensor = current_embeddings[-1]
        elif pooling_operation == "mean":
            all_embeddings: List[torch.FloatTensor] = [
                embedding.unsqueeze(0) for embedding in current_embeddings
            ]
            final_embedding: torch.FloatTensor = torch.mean(
                torch.cat(all_embeddings, dim=0), dim=0
            )
        else:
            final_embedding: torch.FloatTensor = first_embedding

        subtoken_embeddings.append(final_embedding)

    if use_scalar_mix:
        sm = ScalarMix(mixture_size=len(subtoken_embeddings))
        sm_embeddings = sm(subtoken_embeddings)

        subtoken_embeddings = [sm_embeddings]

    return subtoken_embeddings


def _build_token_subwords_mapping(
    sentence: Sentence, tokenizer: PreTrainedTokenizer
) -> Dict[int, int]:
    """ Builds a dictionary that stores the following information:
    Token index (key) and number of corresponding subwords (value) for a sentence.

    :param sentence: input sentence
    :param tokenizer: PyTorch-Transformers tokenization object
    :return: dictionary of token index to corresponding number of subwords
    """
    token_subwords_mapping: Dict[int, int] = {}

    for token in sentence.tokens:
        token_text = token.text

        subwords = tokenizer.tokenize(token_text)

        token_subwords_mapping[token.idx] = len(subwords)

    return token_subwords_mapping


def _build_token_subwords_mapping_gpt2(
    sentence: Sentence, tokenizer: PreTrainedTokenizer
) -> Dict[int, int]:
    """ Builds a dictionary that stores the following information:
    Token index (key) and number of corresponding subwords (value) for a sentence.

    :param sentence: input sentence
    :param tokenizer: PyTorch-Transformers tokenization object
    :return: dictionary of token index to corresponding number of subwords
    """
    token_subwords_mapping: Dict[int, int] = {}

    for token in sentence.tokens:
        # Dummy token is needed to get the actually token tokenized correctly with special ``Ġ`` symbol

        if token.idx == 1:
            token_text = token.text
            subwords = tokenizer.tokenize(token_text)
        else:
            token_text = "X " + token.text
            subwords = tokenizer.tokenize(token_text)[1:]

        token_subwords_mapping[token.idx] = len(subwords)

    return token_subwords_mapping


def _get_transformer_sentence_embeddings(
    sentences: List[Sentence],
    tokenizer: PreTrainedTokenizer,
    model: PreTrainedModel,
    name: str,
    layers: List[int],
    pooling_operation: str,
    use_scalar_mix: bool,
    bos_token: str = None,
    eos_token: str = None,
    gradient_context = torch.no_grad(),
) -> List[Sentence]:
    """
    Builds sentence embeddings for Transformer-based architectures.
    :param sentences: input sentences
    :param tokenizer: tokenization object
    :param model: model object
    :param name: name of the Transformer-based model
    :param layers: list of layers
    :param pooling_operation: defines pooling operation for subword extraction
    :param use_scalar_mix: defines the usage of scalar mix for specified layer(s)
    :param bos_token: defines begin of sentence token (used for left padding)
    :param eos_token: defines end of sentence token (used for right padding)
    :return: list of sentences (each token of a sentence is now embedded)
    """
    with gradient_context:
        for sentence in sentences:
            token_subwords_mapping: Dict[int, int] = {}

            if name.startswith("gpt2") or name.startswith("roberta"):
                token_subwords_mapping = _build_token_subwords_mapping_gpt2(
                    sentence=sentence, tokenizer=tokenizer
                )
            else:
                token_subwords_mapping = _build_token_subwords_mapping(
                    sentence=sentence, tokenizer=tokenizer
                )
            subwords = tokenizer.tokenize(sentence.to_tokenized_string())

            offset = 0

            if 'xlmr' in name:
                offset = 1

            if bos_token:
                subwords = [bos_token] + subwords
                offset = 1

            if eos_token:
                subwords = subwords + [eos_token]

            indexed_tokens = tokenizer.convert_tokens_to_ids(subwords)
            tokens_tensor = torch.tensor([indexed_tokens])
            tokens_tensor = tokens_tensor.to(flair.device)

            hidden_states = model(tokens_tensor)[-1]

            for token in sentence.tokens:
                len_subwords = token_subwords_mapping[token.idx]

                subtoken_embeddings = _extract_embeddings(
                    hidden_states=hidden_states,
                    layers=layers,
                    pooling_operation=pooling_operation,
                    subword_start_idx=offset,
                    subword_end_idx=offset + len_subwords,
                    use_scalar_mix=use_scalar_mix,
                )

                offset += len_subwords

                final_subtoken_embedding = torch.cat(subtoken_embeddings)
                token.set_embedding(name, final_subtoken_embedding)

    return sentences


class TransformerXLEmbeddings(TokenEmbeddings):
    def __init__(
        self,
        pretrained_model_name_or_path: str = "transfo-xl-wt103",
        layers: str = "1,2,3",
        use_scalar_mix: bool = False,
    ):
        """Transformer-XL embeddings, as proposed in Dai et al., 2019.
        :param pretrained_model_name_or_path: name or path of Transformer-XL model
        :param layers: comma-separated list of layers
        :param use_scalar_mix: defines the usage of scalar mix for specified layer(s)
        """
        super().__init__()

        self.tokenizer = TransfoXLTokenizer.from_pretrained(
            pretrained_model_name_or_path
        )
        self.model = TransfoXLModel.from_pretrained(
            pretrained_model_name_or_path=pretrained_model_name_or_path,
            output_hidden_states=True,
        )
        self.name = pretrained_model_name_or_path
        self.layers: List[int] = [int(layer) for layer in layers.split(",")]
        self.use_scalar_mix = use_scalar_mix
        self.static_embeddings = True

        dummy_sentence: Sentence = Sentence()
        dummy_sentence.add_token(Token("hello"))
        embedded_dummy = self.embed(dummy_sentence)
        self.__embedding_length: int = len(
            embedded_dummy[0].get_token(1).get_embedding()
        )

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:
        self.model.to(flair.device)
        self.model.eval()

        sentences = _get_transformer_sentence_embeddings(
            sentences=sentences,
            tokenizer=self.tokenizer,
            model=self.model,
            name=self.name,
            layers=self.layers,
            pooling_operation="first",
            use_scalar_mix=self.use_scalar_mix,
            eos_token="<eos>",
        )
        return sentences

    def extra_repr(self):
        return "model={}".format(self.name)

    def __str__(self):
        return self.name


class XLNetEmbeddings(TokenEmbeddings):
    def __init__(
        self,
        pretrained_model_name_or_path: str = "xlnet-large-cased",
        layers: str = "1",
        pooling_operation: str = "first_last",
        use_scalar_mix: bool = False,
    ):
        """XLNet embeddings, as proposed in Yang et al., 2019.
        :param pretrained_model_name_or_path: name or path of XLNet model
        :param layers: comma-separated list of layers
        :param pooling_operation: defines pooling operation for subwords
        :param use_scalar_mix: defines the usage of scalar mix for specified layer(s)
        """
        super().__init__()

        self.tokenizer = XLNetTokenizer.from_pretrained(pretrained_model_name_or_path)
        self.model = XLNetModel.from_pretrained(
            pretrained_model_name_or_path=pretrained_model_name_or_path,
            output_hidden_states=True,
        )
        self.name = pretrained_model_name_or_path
        self.layers: List[int] = [int(layer) for layer in layers.split(",")]
        self.pooling_operation = pooling_operation
        self.use_scalar_mix = use_scalar_mix
        self.static_embeddings = True

        dummy_sentence: Sentence = Sentence()
        dummy_sentence.add_token(Token("hello"))
        embedded_dummy = self.embed(dummy_sentence)
        self.__embedding_length: int = len(
            embedded_dummy[0].get_token(1).get_embedding()
        )

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:
        self.model.to(flair.device)
        self.model.eval()

        sentences = _get_transformer_sentence_embeddings(
            sentences=sentences,
            tokenizer=self.tokenizer,
            model=self.model,
            name=self.name,
            layers=self.layers,
            pooling_operation=self.pooling_operation,
            use_scalar_mix=self.use_scalar_mix,
            bos_token="<s>",
            eos_token="</s>",
        )

        return sentences

    def extra_repr(self):
        return "model={}".format(self.name)

    def __str__(self):
        return self.name


class XLMEmbeddings(TokenEmbeddings):
    def __init__(
        self,
        pretrained_model_name_or_path: str = "xlm-mlm-en-2048",
        layers: str = "1",
        pooling_operation: str = "first_last",
        use_scalar_mix: bool = False,
    ):
        """
        XLM embeddings, as proposed in Guillaume et al., 2019.
        :param pretrained_model_name_or_path: name or path of XLM model
        :param layers: comma-separated list of layers
        :param pooling_operation: defines pooling operation for subwords
        :param use_scalar_mix: defines the usage of scalar mix for specified layer(s)
        """
        super().__init__()

        self.tokenizer = XLMTokenizer.from_pretrained(pretrained_model_name_or_path)
        self.model = XLMModel.from_pretrained(
            pretrained_model_name_or_path=pretrained_model_name_or_path,
            output_hidden_states=True,
        )
        self.name = pretrained_model_name_or_path
        self.layers: List[int] = [int(layer) for layer in layers.split(",")]
        self.pooling_operation = pooling_operation
        self.use_scalar_mix = use_scalar_mix
        self.static_embeddings = True

        dummy_sentence: Sentence = Sentence()
        dummy_sentence.add_token(Token("hello"))
        embedded_dummy = self.embed(dummy_sentence)
        self.__embedding_length: int = len(
            embedded_dummy[0].get_token(1).get_embedding()
        )

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:
        self.model.to(flair.device)
        self.model.eval()

        sentences = _get_transformer_sentence_embeddings(
            sentences=sentences,
            tokenizer=self.tokenizer,
            model=self.model,
            name=self.name,
            layers=self.layers,
            pooling_operation=self.pooling_operation,
            use_scalar_mix=self.use_scalar_mix,
            bos_token="<s>",
            eos_token="</s>",
        )

        return sentences

    def extra_repr(self):
        return "model={}".format(self.name)

    def __str__(self):
        return self.name


class OpenAIGPTEmbeddings(TokenEmbeddings):
    def __init__(
        self,
        pretrained_model_name_or_path: str = "openai-gpt",
        layers: str = "1",
        pooling_operation: str = "first_last",
        use_scalar_mix: bool = False,
    ):
        """OpenAI GPT embeddings, as proposed in Radford et al. 2018.
        :param pretrained_model_name_or_path: name or path of OpenAI GPT model
        :param layers: comma-separated list of layers
        :param pooling_operation: defines pooling operation for subwords
        :param use_scalar_mix: defines the usage of scalar mix for specified layer(s)
        """
        super().__init__()

        self.tokenizer = OpenAIGPTTokenizer.from_pretrained(
            pretrained_model_name_or_path
        )
        self.model = OpenAIGPTModel.from_pretrained(
            pretrained_model_name_or_path=pretrained_model_name_or_path,
            output_hidden_states=True,
        )
        self.name = pretrained_model_name_or_path
        self.layers: List[int] = [int(layer) for layer in layers.split(",")]
        self.pooling_operation = pooling_operation
        self.use_scalar_mix = use_scalar_mix
        self.static_embeddings = True

        dummy_sentence: Sentence = Sentence()
        dummy_sentence.add_token(Token("hello"))
        embedded_dummy = self.embed(dummy_sentence)
        self.__embedding_length: int = len(
            embedded_dummy[0].get_token(1).get_embedding()
        )

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:
        self.model.to(flair.device)
        self.model.eval()

        sentences = _get_transformer_sentence_embeddings(
            sentences=sentences,
            tokenizer=self.tokenizer,
            model=self.model,
            name=self.name,
            layers=self.layers,
            pooling_operation=self.pooling_operation,
            use_scalar_mix=self.use_scalar_mix,
        )

        return sentences

    def extra_repr(self):
        return "model={}".format(self.name)

    def __str__(self):
        return self.name


class OpenAIGPT2Embeddings(TokenEmbeddings):
    def __init__(
        self,
        pretrained_model_name_or_path: str = "gpt2-medium",
        layers: str = "1",
        pooling_operation: str = "first_last",
        use_scalar_mix: bool = False,
    ):
        """OpenAI GPT-2 embeddings, as proposed in Radford et al. 2019.
        :param pretrained_model_name_or_path: name or path of OpenAI GPT-2 model
        :param layers: comma-separated list of layers
        :param pooling_operation: defines pooling operation for subwords
        :param use_scalar_mix: defines the usage of scalar mix for specified layer(s)
        """
        super().__init__()

        self.tokenizer = GPT2Tokenizer.from_pretrained(pretrained_model_name_or_path)
        self.model = GPT2Model.from_pretrained(
            pretrained_model_name_or_path=pretrained_model_name_or_path,
            output_hidden_states=True,
        )
        self.name = pretrained_model_name_or_path
        self.layers: List[int] = [int(layer) for layer in layers.split(",")]
        self.pooling_operation = pooling_operation
        self.use_scalar_mix = use_scalar_mix
        self.static_embeddings = True

        dummy_sentence: Sentence = Sentence()
        dummy_sentence.add_token(Token("hello"))
        embedded_dummy = self.embed(dummy_sentence)
        self.__embedding_length: int = len(
            embedded_dummy[0].get_token(1).get_embedding()
        )

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:
        self.model.to(flair.device)
        self.model.eval()

        sentences = _get_transformer_sentence_embeddings(
            sentences=sentences,
            tokenizer=self.tokenizer,
            model=self.model,
            name=self.name,
            layers=self.layers,
            pooling_operation=self.pooling_operation,
            use_scalar_mix=self.use_scalar_mix,
            bos_token="<|endoftext|>",
            eos_token="<|endoftext|>",
        )

        return sentences


class RoBERTaEmbeddings(TokenEmbeddings):
    def __init__(
        self,
        pretrained_model_name_or_path: str = "roberta-base",
        layers: str = "-1",
        pooling_operation: str = "first",
        use_scalar_mix: bool = False,
    ):
        """RoBERTa, as proposed by Liu et al. 2019.
        :param pretrained_model_name_or_path: name or path of RoBERTa model
        :param layers: comma-separated list of layers
        :param pooling_operation: defines pooling operation for subwords
        :param use_scalar_mix: defines the usage of scalar mix for specified layer(s)
        """
        super().__init__()

        self.tokenizer = RobertaTokenizer.from_pretrained(pretrained_model_name_or_path)
        self.model = RobertaModel.from_pretrained(
            pretrained_model_name_or_path=pretrained_model_name_or_path,
            output_hidden_states=True,
        )
        self.name = pretrained_model_name_or_path
        self.layers: List[int] = [int(layer) for layer in layers.split(",")]
        self.pooling_operation = pooling_operation
        self.use_scalar_mix = use_scalar_mix
        self.static_embeddings = True

        dummy_sentence: Sentence = Sentence()
        dummy_sentence.add_token(Token("hello"))
        embedded_dummy = self.embed(dummy_sentence)
        self.__embedding_length: int = len(
            embedded_dummy[0].get_token(1).get_embedding()
        )

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:
        self.model.to(flair.device)
        self.model.eval()

        sentences = _get_transformer_sentence_embeddings(
            sentences=sentences,
            tokenizer=self.tokenizer,
            model=self.model,
            name=self.name,
            layers=self.layers,
            pooling_operation=self.pooling_operation,
            use_scalar_mix=self.use_scalar_mix,
            bos_token="<s>",
            eos_token="</s>",
        )

        return sentences


class XLMRoBERTaEmbeddings(TokenEmbeddings):
    def __init__(
        self,
        pretrained_model_name_or_path: str = "xlm-roberta-large",
        layers: str = "-1",
        pooling_operation: str = "first",
        fine_tune: bool = False,
        use_scalar_mix: bool = False,
    ):
        """RoBERTa, as proposed by Liu et al. 2019.
        :param pretrained_model_name_or_path: name or path of RoBERTa model
        :param layers: comma-separated list of layers
        :param pooling_operation: defines pooling operation for subwords
        :param use_scalar_mix: defines the usage of scalar mix for specified layer(s)
        """
        super().__init__()
        self.tokenizer = XLMRobertaTokenizer.from_pretrained(pretrained_model_name_or_path)
        self.model = XLMRobertaModel.from_pretrained(
            pretrained_model_name_or_path=pretrained_model_name_or_path,
            output_hidden_states=True,
        )
        
        self.name = pretrained_model_name_or_path
        self.layers: List[int] = [int(layer) for layer in layers.split(",")]
        self.pooling_operation = pooling_operation
        self.use_scalar_mix = use_scalar_mix

        dummy_sentence: Sentence = Sentence()
        dummy_sentence.add_token(Token("hello"))
        embedded_dummy = self.embed(dummy_sentence)
        self.__embedding_length: int = len(
            embedded_dummy[0].get_token(1).get_embedding()
        )
        self.fine_tune = fine_tune
        self.static_embeddings = not self.fine_tune
        if self.static_embeddings:
            self.model.eval()
    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:
        if not hasattr(self,'fine_tune'):
            self.fine_tune=False
        if hasattr(sentences, 'features'):
            if not self.fine_tune:
                if self.name in sentences.features:
                    return sentences
                if len(sentences)>0:
                    if self.name in sentences[0][0]._embeddings.keys():
                        sentences = self.assign_batch_features(sentences)
                        return sentences
        
        self.model.to(flair.device)
        if not self.fine_tune:
            self.model.eval()
        else:
            self.model.train()
        
        gradient_context = torch.enable_grad() if self.fine_tune else torch.no_grad()
        sentences = _get_transformer_sentence_embeddings(
            sentences=sentences,
            tokenizer=self.tokenizer,
            model=self.model,
            name=self.name,
            layers=self.layers,
            pooling_operation=self.pooling_operation,
            use_scalar_mix=self.use_scalar_mix,
            bos_token="<s>",
            eos_token="</s>",
            gradient_context=gradient_context,
        )
        # pdb.set_trace()
        if hasattr(sentences, 'features'):
            sentences = self.assign_batch_features(sentences)
        
        return sentences

    def extra_repr(self):
        return "model={}".format(self.name)

    def __str__(self):
        return self.name




class CharacterEmbeddings(TokenEmbeddings):
    """Character embeddings of words, as proposed in Lample et al., 2016."""

    def __init__(
        self,
        path_to_char_dict: str = None,
        char_embedding_dim: int = 25,
        hidden_size_char: int = 25,
    ):
        """Uses the default character dictionary if none provided."""

        super().__init__()
        self.name = "Char"
        self.static_embeddings = False

        # use list of common characters if none provided
        if path_to_char_dict is None:
            self.char_dictionary: Dictionary = Dictionary.load("common-chars")
        else:
            self.char_dictionary: Dictionary = Dictionary.load_from_file(
                path_to_char_dict
            )

        self.char_embedding_dim: int = char_embedding_dim
        self.hidden_size_char: int = hidden_size_char
        self.char_embedding = torch.nn.Embedding(
            len(self.char_dictionary.item2idx), self.char_embedding_dim
        )
        self.char_rnn = torch.nn.LSTM(
            self.char_embedding_dim,
            self.hidden_size_char,
            num_layers=1,
            bidirectional=True,
        )

        self.__embedding_length = self.char_embedding_dim * 2

        self.to(flair.device)

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def _add_embeddings_internal(self, sentences: List[Sentence]):

        for sentence in sentences:

            tokens_char_indices = []

            # translate words in sentence into ints using dictionary
            for token in sentence.tokens:
                char_indices = [
                    self.char_dictionary.get_idx_for_item(char) for char in token.text
                ]
                tokens_char_indices.append(char_indices)

            # sort words by length, for batching and masking
            tokens_sorted_by_length = sorted(
                tokens_char_indices, key=lambda p: len(p), reverse=True
            )
            d = {}
            for i, ci in enumerate(tokens_char_indices):
                for j, cj in enumerate(tokens_sorted_by_length):
                    if ci == cj:
                        d[j] = i
                        continue
            chars2_length = [len(c) for c in tokens_sorted_by_length]
            longest_token_in_sentence = max(chars2_length)
            tokens_mask = torch.zeros(
                (len(tokens_sorted_by_length), longest_token_in_sentence),
                dtype=torch.long,
                device=flair.device,
            )

            for i, c in enumerate(tokens_sorted_by_length):
                tokens_mask[i, : chars2_length[i]] = torch.tensor(
                    c, dtype=torch.long, device=flair.device
                )

            # chars for rnn processing
            chars = tokens_mask

            character_embeddings = self.char_embedding(chars).transpose(0, 1)

            packed = torch.nn.utils.rnn.pack_padded_sequence(
                character_embeddings, chars2_length
            )

            lstm_out, self.hidden = self.char_rnn(packed)

            outputs, output_lengths = torch.nn.utils.rnn.pad_packed_sequence(lstm_out)
            outputs = outputs.transpose(0, 1)
            chars_embeds_temp = torch.zeros(
                (outputs.size(0), outputs.size(2)),
                dtype=torch.float,
                device=flair.device,
            )
            for i, index in enumerate(output_lengths):
                chars_embeds_temp[i] = outputs[i, index - 1]
            character_embeddings = chars_embeds_temp.clone()
            for i in range(character_embeddings.size(0)):
                character_embeddings[d[i]] = chars_embeds_temp[i]

            for token_number, token in enumerate(sentence.tokens):
                token.set_embedding(self.name, character_embeddings[token_number])

    def __str__(self):
        return self.name


class FlairEmbeddings(TokenEmbeddings):
    """Contextual string embeddings of words, as proposed in Akbik et al., 2018."""

    def __init__(self,
                 model,
                 fine_tune: bool = False,
                 chars_per_chunk: int = 512,
                 with_whitespace: bool = True,
                 tokenized_lm: bool = True,
                 ):
        """
        initializes contextual string embeddings using a character-level language model.
        :param model: model string, one of 'news-forward', 'news-backward', 'news-forward-fast', 'news-backward-fast',
                'mix-forward', 'mix-backward', 'german-forward', 'german-backward', 'polish-backward', 'polish-forward',
                etc (see https://github.com/flairNLP/flair/blob/master/resources/docs/embeddings/FLAIR_EMBEDDINGS.md)
                depending on which character language model is desired.
        :param fine_tune: if set to True, the gradient will propagate into the language model. This dramatically slows
                down training and often leads to overfitting, so use with caution.
        :param chars_per_chunk: max number of chars per rnn pass to control speed/memory tradeoff. Higher means faster
                but requires more memory. Lower means slower but less memory.
        :param with_whitespace: If True, use hidden state after whitespace after word. If False, use hidden
                 state at last character of word.
        :param tokenized_lm: Whether this lm is tokenized. Default is True, but for LMs trained over unprocessed text
                False might be better.
        """
        super().__init__()
        self.instance_parameters = self.get_instance_parameters(locals=locals())

        cache_dir = Path("embeddings")

        hu_path: str = "https://flair.informatik.hu-berlin.de/resources/embeddings/flair"
        clef_hipe_path: str = "https://files.ifi.uzh.ch/cl/siclemat/impresso/clef-hipe-2020/flair"

        self.PRETRAINED_MODEL_ARCHIVE_MAP = {
            # multilingual models
            "multi-forward": f"{hu_path}/lm-jw300-forward-v0.1.pt",
            "multi-backward": f"{hu_path}/lm-jw300-backward-v0.1.pt",
            "multi-v0-forward": f"{hu_path}/lm-multi-forward-v0.1.pt",
            "multi-v0-backward": f"{hu_path}/lm-multi-backward-v0.1.pt",
            "multi-v0-forward-fast": f"{hu_path}/lm-multi-forward-fast-v0.1.pt",
            "multi-v0-backward-fast": f"{hu_path}/lm-multi-backward-fast-v0.1.pt",
            # English models
            "en-forward": f"{hu_path}/news-forward-0.4.1.pt",
            "en-backward": f"{hu_path}/news-backward-0.4.1.pt",
            "en-forward-fast": f"{hu_path}/lm-news-english-forward-1024-v0.2rc.pt",
            "en-backward-fast": f"{hu_path}/lm-news-english-backward-1024-v0.2rc.pt",
            "news-forward": f"{hu_path}/news-forward-0.4.1.pt",
            "news-backward": f"{hu_path}/news-backward-0.4.1.pt",
            "news-forward-fast": f"{hu_path}/lm-news-english-forward-1024-v0.2rc.pt",
            "news-backward-fast": f"{hu_path}/lm-news-english-backward-1024-v0.2rc.pt",
            "mix-forward": f"{hu_path}/lm-mix-english-forward-v0.2rc.pt",
            "mix-backward": f"{hu_path}/lm-mix-english-backward-v0.2rc.pt",
            # Arabic
            "ar-forward": f"{hu_path}/lm-ar-opus-large-forward-v0.1.pt",
            "ar-backward": f"{hu_path}/lm-ar-opus-large-backward-v0.1.pt",
            # Bulgarian
            "bg-forward-fast": f"{hu_path}/lm-bg-small-forward-v0.1.pt",
            "bg-backward-fast": f"{hu_path}/lm-bg-small-backward-v0.1.pt",
            "bg-forward": f"{hu_path}/lm-bg-opus-large-forward-v0.1.pt",
            "bg-backward": f"{hu_path}/lm-bg-opus-large-backward-v0.1.pt",
            # Czech
            "cs-forward": f"{hu_path}/lm-cs-opus-large-forward-v0.1.pt",
            "cs-backward": f"{hu_path}/lm-cs-opus-large-backward-v0.1.pt",
            "cs-v0-forward": f"{hu_path}/lm-cs-large-forward-v0.1.pt",
            "cs-v0-backward": f"{hu_path}/lm-cs-large-backward-v0.1.pt",
            # Danish
            "da-forward": f"{hu_path}/lm-da-opus-large-forward-v0.1.pt",
            "da-backward": f"{hu_path}/lm-da-opus-large-backward-v0.1.pt",
            # German
            "de-forward": f"{hu_path}/lm-mix-german-forward-v0.2rc.pt",
            "de-backward": f"{hu_path}/lm-mix-german-backward-v0.2rc.pt",
            "de-historic-ha-forward": f"{hu_path}/lm-historic-hamburger-anzeiger-forward-v0.1.pt",
            "de-historic-ha-backward": f"{hu_path}/lm-historic-hamburger-anzeiger-backward-v0.1.pt",
            "de-historic-wz-forward": f"{hu_path}/lm-historic-wiener-zeitung-forward-v0.1.pt",
            "de-historic-wz-backward": f"{hu_path}/lm-historic-wiener-zeitung-backward-v0.1.pt",
            "de-historic-rw-forward": f"{hu_path}/redewiedergabe_lm_forward.pt",
            "de-historic-rw-backward": f"{hu_path}/redewiedergabe_lm_backward.pt",
            # Spanish
            "es-forward": f"{hu_path}/lm-es-forward.pt",
            "es-backward": f"{hu_path}/lm-es-backward.pt",
            "es-forward-fast": f"{hu_path}/lm-es-forward-fast.pt",
            "es-backward-fast": f"{hu_path}/lm-es-backward-fast.pt",
            # Basque
            "eu-forward": f"{hu_path}/lm-eu-opus-large-forward-v0.2.pt",
            "eu-backward": f"{hu_path}/lm-eu-opus-large-backward-v0.2.pt",
            "eu-v1-forward": f"{hu_path}/lm-eu-opus-large-forward-v0.1.pt",
            "eu-v1-backward": f"{hu_path}/lm-eu-opus-large-backward-v0.1.pt",
            "eu-v0-forward": f"{hu_path}/lm-eu-large-forward-v0.1.pt",
            "eu-v0-backward": f"{hu_path}/lm-eu-large-backward-v0.1.pt",
            # Persian
            "fa-forward": f"{hu_path}/lm-fa-opus-large-forward-v0.1.pt",
            "fa-backward": f"{hu_path}/lm-fa-opus-large-backward-v0.1.pt",
            # Finnish
            "fi-forward": f"{hu_path}/lm-fi-opus-large-forward-v0.1.pt",
            "fi-backward": f"{hu_path}/lm-fi-opus-large-backward-v0.1.pt",
            # French
            "fr-forward": f"{hu_path}/lm-fr-charlm-forward.pt",
            "fr-backward": f"{hu_path}/lm-fr-charlm-backward.pt",
            # Hebrew
            "he-forward": f"{hu_path}/lm-he-opus-large-forward-v0.1.pt",
            "he-backward": f"{hu_path}/lm-he-opus-large-backward-v0.1.pt",
            # Hindi
            "hi-forward": f"{hu_path}/lm-hi-opus-large-forward-v0.1.pt",
            "hi-backward": f"{hu_path}/lm-hi-opus-large-backward-v0.1.pt",
            # Croatian
            "hr-forward": f"{hu_path}/lm-hr-opus-large-forward-v0.1.pt",
            "hr-backward": f"{hu_path}/lm-hr-opus-large-backward-v0.1.pt",
            # Indonesian
            "id-forward": f"{hu_path}/lm-id-opus-large-forward-v0.1.pt",
            "id-backward": f"{hu_path}/lm-id-opus-large-backward-v0.1.pt",
            # Italian
            "it-forward": f"{hu_path}/lm-it-opus-large-forward-v0.1.pt",
            "it-backward": f"{hu_path}/lm-it-opus-large-backward-v0.1.pt",
            # Japanese
            "ja-forward": f"{hu_path}/japanese-forward.pt",
            "ja-backward": f"{hu_path}/japanese-backward.pt",
            # Malayalam
            "ml-forward": f"https://raw.githubusercontent.com/qburst/models-repository/master/FlairMalayalamModels/ml-forward.pt",
            "ml-backward": f"https://raw.githubusercontent.com/qburst/models-repository/master/FlairMalayalamModels/ml-backward.pt",
            # Dutch
            "nl-forward": f"{hu_path}/lm-nl-opus-large-forward-v0.1.pt",
            "nl-backward": f"{hu_path}/lm-nl-opus-large-backward-v0.1.pt",
            "nl-v0-forward": f"{hu_path}/lm-nl-large-forward-v0.1.pt",
            "nl-v0-backward": f"{hu_path}/lm-nl-large-backward-v0.1.pt",
            # Norwegian
            "no-forward": f"{hu_path}/lm-no-opus-large-forward-v0.1.pt",
            "no-backward": f"{hu_path}/lm-no-opus-large-backward-v0.1.pt",
            # Polish
            "pl-forward": f"{hu_path}/lm-polish-forward-v0.2.pt",
            "pl-backward": f"{hu_path}/lm-polish-backward-v0.2.pt",
            "pl-opus-forward": f"{hu_path}/lm-pl-opus-large-forward-v0.1.pt",
            "pl-opus-backward": f"{hu_path}/lm-pl-opus-large-backward-v0.1.pt",
            # Portuguese
            "pt-forward": f"{hu_path}/lm-pt-forward.pt",
            "pt-backward": f"{hu_path}/lm-pt-backward.pt",
            # Pubmed
            "pubmed-forward": f"{hu_path}/pubmed-forward.pt",
            "pubmed-backward": f"{hu_path}/pubmed-backward.pt",
            "pubmed-2015-forward": f"{hu_path}/pubmed-2015-fw-lm.pt",
            "pubmed-2015-backward": f"{hu_path}/pubmed-2015-bw-lm.pt",
            # Slovenian
            "sl-forward": f"{hu_path}/lm-sl-opus-large-forward-v0.1.pt",
            "sl-backward": f"{hu_path}/lm-sl-opus-large-backward-v0.1.pt",
            "sl-v0-forward": f"{hu_path}/lm-sl-large-forward-v0.1.pt",
            "sl-v0-backward": f"{hu_path}/lm-sl-large-backward-v0.1.pt",
            # Swedish
            "sv-forward": f"{hu_path}/lm-sv-opus-large-forward-v0.1.pt",
            "sv-backward": f"{hu_path}/lm-sv-opus-large-backward-v0.1.pt",
            "sv-v0-forward": f"{hu_path}/lm-sv-large-forward-v0.1.pt",
            "sv-v0-backward": f"{hu_path}/lm-sv-large-backward-v0.1.pt",
            # Tamil
            "ta-forward": f"{hu_path}/lm-ta-opus-large-forward-v0.1.pt",
            "ta-backward": f"{hu_path}/lm-ta-opus-large-backward-v0.1.pt",
            # CLEF HIPE Shared task
            "de-impresso-hipe-v1-forward": f"{clef_hipe_path}/de-hipe-flair-v1-forward/best-lm.pt",
            "de-impresso-hipe-v1-backward": f"{clef_hipe_path}/de-hipe-flair-v1-backward/best-lm.pt",
            "en-impresso-hipe-v1-forward": f"{clef_hipe_path}/en-flair-v1-forward/best-lm.pt",
            "en-impresso-hipe-v1-backward": f"{clef_hipe_path}/en-flair-v1-backward/best-lm.pt",
            "fr-impresso-hipe-v1-forward": f"{clef_hipe_path}/fr-hipe-flair-v1-forward/best-lm.pt",
            "fr-impresso-hipe-v1-backward": f"{clef_hipe_path}/fr-hipe-flair-v1-backward/best-lm.pt",
        }

        if type(model) == str:

            # load model if in pretrained model map
            if model.lower() in self.PRETRAINED_MODEL_ARCHIVE_MAP:
                base_path = self.PRETRAINED_MODEL_ARCHIVE_MAP[model.lower()]

                # Fix for CLEF HIPE models (avoid overwriting best-lm.pt in cache_dir)
                if "impresso-hipe" in model.lower():
                    cache_dir = cache_dir / model.lower()
                model = cached_path(base_path, cache_dir=cache_dir)

            elif replace_with_language_code(model) in self.PRETRAINED_MODEL_ARCHIVE_MAP:
                base_path = self.PRETRAINED_MODEL_ARCHIVE_MAP[
                    replace_with_language_code(model)
                ]
                model = cached_path(base_path, cache_dir=cache_dir)

            elif not Path(model).exists():
                raise ValueError(
                    f'The given model "{model}" is not available or is not a valid path.'
                )

        from flair.models import LanguageModel

        if type(model) == LanguageModel:
            self.lm: LanguageModel = model
            self.name = f"Task-LSTM-{self.lm.hidden_size}-{self.lm.nlayers}-{self.lm.is_forward_lm}"
        else:
            self.lm: LanguageModel = LanguageModel.load_language_model(model)
            self.name = str(model)

        # embeddings are static if we don't do finetuning
        self.fine_tune = fine_tune
        self.static_embeddings = not fine_tune

        self.is_forward_lm: bool = self.lm.is_forward_lm
        self.with_whitespace: bool = with_whitespace
        self.tokenized_lm: bool = tokenized_lm
        self.chars_per_chunk: int = chars_per_chunk

        # embed a dummy sentence to determine embedding_length
        dummy_sentence: Sentence = Sentence()
        dummy_sentence.add_token(Token("hello"))
        embedded_dummy = self.embed(dummy_sentence)
        self.__embedding_length: int = len(
            embedded_dummy[0].get_token(1).get_embedding()
        )

        # set to eval mode
        self.eval()

    def train(self, mode=True):

        # make compatible with serialized models (TODO: remove)
        if "fine_tune" not in self.__dict__:
            self.fine_tune = False
        if "chars_per_chunk" not in self.__dict__:
            self.chars_per_chunk = 512

        if not self.fine_tune:
            pass
        else:
            super(FlairEmbeddings, self).train(mode)

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:

        # make compatible with serialized models (TODO: remove)
        if "with_whitespace" not in self.__dict__:
            self.with_whitespace = True
        if "tokenized_lm" not in self.__dict__:
            self.tokenized_lm = True

        # gradients are enable if fine-tuning is enabled
        gradient_context = torch.enable_grad() if self.fine_tune else torch.no_grad()

        with gradient_context:

            # if this is not possible, use LM to generate embedding. First, get text sentences
            text_sentences = [sentence.to_tokenized_string() for sentence in sentences] if self.tokenized_lm \
                else [sentence.to_plain_string() for sentence in sentences]

            start_marker = self.lm.document_delimiter if "document_delimiter" in self.lm.__dict__ else '\n'
            end_marker = " "

            # get hidden states from language model
            all_hidden_states_in_lm = self.lm.get_representation(
                text_sentences, start_marker, end_marker, self.chars_per_chunk
            )

            if not self.fine_tune:
                all_hidden_states_in_lm = all_hidden_states_in_lm.detach()

            # take first or last hidden states from language model as word representation
            for i, sentence in enumerate(sentences):
                sentence_text = sentence.to_tokenized_string() if self.tokenized_lm else sentence.to_plain_string()

                offset_forward: int = len(start_marker)
                offset_backward: int = len(sentence_text) + len(start_marker)

                for token in sentence.tokens:

                    offset_forward += len(token.text)
                    if self.is_forward_lm:
                        offset_with_whitespace = offset_forward
                        offset_without_whitespace = offset_forward - 1
                    else:
                        offset_with_whitespace = offset_backward
                        offset_without_whitespace = offset_backward - 1

                    # offset mode that extracts at whitespace after last character
                    if self.with_whitespace:
                        embedding = all_hidden_states_in_lm[offset_with_whitespace, i, :]
                    # offset mode that extracts at last character
                    else:
                        embedding = all_hidden_states_in_lm[offset_without_whitespace, i, :]

                    if self.tokenized_lm or token.whitespace_after:
                        offset_forward += 1
                        offset_backward -= 1

                    offset_backward -= len(token.text)

                    # only clone if optimization mode is 'gpu'
                    if flair.embedding_storage_mode == "gpu":
                        embedding = embedding.clone()

                    token.set_embedding(self.name, embedding)

            del all_hidden_states_in_lm

        return sentences

    def __str__(self):
        return self.name


class PooledFlairEmbeddings(TokenEmbeddings):
    def __init__(
        self,
        contextual_embeddings: Union[str, FlairEmbeddings],
        pooling: str = "min",
        only_capitalized: bool = False,
        **kwargs,
    ):

        super().__init__()

        # use the character language model embeddings as basis
        if type(contextual_embeddings) is str:
            self.context_embeddings: FlairEmbeddings = FlairEmbeddings(
                contextual_embeddings, **kwargs
            )
        else:
            self.context_embeddings: FlairEmbeddings = contextual_embeddings

        # length is twice the original character LM embedding length
        self.embedding_length = self.context_embeddings.embedding_length * 2
        self.name = self.context_embeddings.name + "-context"

        # these fields are for the embedding memory
        self.word_embeddings = {}
        self.word_count = {}

        # whether to add only capitalized words to memory (faster runtime and lower memory consumption)
        self.only_capitalized = only_capitalized

        # we re-compute embeddings dynamically at each epoch
        self.static_embeddings = False

        # set the memory method
        self.pooling = pooling
        if pooling == "mean":
            self.aggregate_op = torch.add
        elif pooling == "fade":
            self.aggregate_op = torch.add
        elif pooling == "max":
            self.aggregate_op = torch.max
        elif pooling == "min":
            self.aggregate_op = torch.min

    def train(self, mode=True):
        super().train(mode=mode)
        if mode:
            # memory is wiped each time we do a training run
            print("train mode resetting embeddings")
            self.word_embeddings = {}
            self.word_count = {}

    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:

        # if not hasattr(self,'fine_tune'):
        #     self.fine_tune=False
        # if hasattr(sentences, 'features'):
        #     if not self.fine_tune:
        #         if self.name in sentences.features:
        #             return sentences
        #         if len(sentences)>0:
        #             if self.name in sentences[0][0]._embeddings.keys():
        #                 sentences = self.assign_batch_features(sentences, embedding_length = self.context_embeddings.embedding_length)
        #                 return sentences

        self.context_embeddings.embed(sentences)

        # if we keep a pooling, it needs to be updated continuously
        for sentence in sentences:
            for token in sentence.tokens:

                # update embedding
                local_embedding = token._embeddings[self.context_embeddings.name]
                local_embedding = local_embedding.to(flair.device)

                if token.text[0].isupper() or not self.only_capitalized:

                    if token.text not in self.word_embeddings:
                        self.word_embeddings[token.text] = local_embedding
                        self.word_count[token.text] = 1
                    else:
                        aggregated_embedding = self.aggregate_op(
                            self.word_embeddings[token.text], local_embedding
                        )
                        if self.pooling == "fade":
                            aggregated_embedding /= 2
                        self.word_embeddings[token.text] = aggregated_embedding
                        self.word_count[token.text] += 1

        # add embeddings after updating
        for sentence in sentences:
            for token in sentence.tokens:
                if token.text in self.word_embeddings:
                    base = (
                        self.word_embeddings[token.text] / self.word_count[token.text]
                        if self.pooling == "mean"
                        else self.word_embeddings[token.text]
                    )
                else:
                    base = token._embeddings[self.context_embeddings.name]

                token.set_embedding(self.name, base)
        if hasattr(sentences, 'features'):
            sentences = self.assign_batch_features(sentences, embedding_length = self.context_embeddings.embedding_length)
        return sentences

    def embedding_length(self) -> int:
        return self.embedding_length


class BertEmbeddings(TokenEmbeddings):
    def __init__(
        self,
        bert_model_or_path: str = "bert-base-uncased",
        layers: str = "-1,-2,-3,-4",
        pooling_operation: str = "first",
        use_scalar_mix: bool = False,
        fine_tune: bool = False,
        sentence_feat: bool = False,
    ):
        """
        Bidirectional transformer embeddings of words, as proposed in Devlin et al., 2018.
        :param bert_model_or_path: name of BERT model ('') or directory path containing custom model, configuration file
        and vocab file (names of three files should be - config.json, pytorch_model.bin/model.chkpt, vocab.txt)
        :param layers: string indicating which layers to take for embedding
        :param pooling_operation: how to get from token piece embeddings to token embedding. Either pool them and take
        the average ('mean') or use first word piece embedding as token embedding ('first)
        """
        super().__init__()

        self.tokenizer = BertTokenizer.from_pretrained(bert_model_or_path)
        self.model = BertModel.from_pretrained(pretrained_model_name_or_path=bert_model_or_path, output_hidden_states=True)
        self.layer_indexes = [int(x) for x in layers.split(",")]
        self.pooling_operation = pooling_operation
        self.use_scalar_mix = use_scalar_mix
        self.name = str(bert_model_or_path)
        self.fine_tune = fine_tune
        self.static_embeddings = not self.fine_tune
        if self.static_embeddings:
            self.model.eval()  # disable dropout (or leave in train mode to finetune)
        # if True, return the sentence_feat
        self.sentence_feat=sentence_feat

    class BertInputFeatures(object):
        """Private helper class for holding BERT-formatted features"""

        def __init__(
            self,
            unique_id,
            tokens,
            input_ids,
            input_mask,
            input_type_ids,
            token_subtoken_count,
        ):
            self.unique_id = unique_id
            self.tokens = tokens
            self.input_ids = input_ids
            self.input_mask = input_mask
            self.input_type_ids = input_type_ids
            self.token_subtoken_count = token_subtoken_count

    def _convert_sentences_to_features(
        self, sentences, max_sequence_length: int
    ) -> [BertInputFeatures]:

        max_sequence_length = max_sequence_length + 2

        features: List[BertEmbeddings.BertInputFeatures] = []
        for (sentence_index, sentence) in enumerate(sentences):

            bert_tokenization: List[str] = []
            token_subtoken_count: Dict[int, int] = {}

            for token in sentence:
                subtokens = self.tokenizer.tokenize(token.text)
                bert_tokenization.extend(subtokens)
                token_subtoken_count[token.idx] = len(subtokens)
            if len(bert_tokenization) > max_sequence_length - 2:
                bert_tokenization = bert_tokenization[0 : (max_sequence_length - 2)]

            tokens = []
            input_type_ids = []
            tokens.append("[CLS]")
            input_type_ids.append(0)
            for token in bert_tokenization:
                tokens.append(token)
                input_type_ids.append(0)
            tokens.append("[SEP]")
            input_type_ids.append(0)
            input_ids = self.tokenizer.convert_tokens_to_ids(tokens)
            # The mask has 1 for real tokens and 0 for padding tokens. Only real
            # tokens are attended to.
            input_mask = [1] * len(input_ids)

            # Zero-pad up to the sequence length.
            while len(input_ids) < max_sequence_length:
                input_ids.append(0)
                input_mask.append(0)
                input_type_ids.append(0)
            features.append(
                BertEmbeddings.BertInputFeatures(
                    unique_id=sentence_index,
                    tokens=tokens,
                    input_ids=input_ids,
                    input_mask=input_mask,
                    input_type_ids=input_type_ids,
                    token_subtoken_count=token_subtoken_count,
                )
            )

        return features

    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:
        """Add embeddings to all words in a list of sentences. If embeddings are already added,
        updates only if embeddings are non-static."""

        if not hasattr(self,'fine_tune'):
            self.fine_tune=False
        if hasattr(sentences, 'features'):
            if not self.fine_tune:
                if self.name in sentences.features:
                    return sentences
                if len(sentences)>0:
                    if self.name in sentences[0][0]._embeddings.keys():
                        sentences = self.assign_batch_features(sentences)
                        return sentences

        # first, find longest sentence in batch
        try:
            longest_sentence_in_batch: int = len(
                max(
                    [
                        self.tokenizer.tokenize(sentence.to_tokenized_string())
                        for sentence in sentences
                    ],
                    key=len,
                )
            )
        except:
            pdb.set_trace()
        if longest_sentence_in_batch>510:
            longest_sentence_in_batch=510
        # prepare id maps for BERT model
        features = self._convert_sentences_to_features(
            sentences, longest_sentence_in_batch
        )
        all_input_ids = torch.LongTensor([f.input_ids for f in features]).to(
            flair.device
        )
        all_input_masks = torch.LongTensor([f.input_mask for f in features]).to(
            flair.device
        )


        # put encoded batch through BERT model to get all hidden states of all encoder layers
        self.model.to(flair.device)
        # self.model.eval()
        if not self.fine_tune:
            self.model.eval()
        gradient_context = torch.enable_grad() if self.fine_tune else torch.no_grad()

        with gradient_context:
            sequence_output, pooled_output, all_encoder_layers = self.model(all_input_ids, token_type_ids=None, attention_mask=all_input_masks)
            # gradients are enable if fine-tuning is enabled
            if not hasattr(self,'sentence_feat'):
                self.sentence_feat=False
            if self.sentence_feat:
                self.pooled_output=pooled_output

            for sentence_index, sentence in enumerate(sentences):

                feature = features[sentence_index]

                # get aggregated embeddings for each BERT-subtoken in sentence
                subtoken_embeddings = []
                for token_index, _ in enumerate(feature.tokens):
                    all_layers = []
                    for layer_index in self.layer_indexes:
                        if self.use_scalar_mix:
                            layer_output = all_encoder_layers[int(layer_index)][
                                sentence_index
                            ]
                        else:
                            if not self.fine_tune:
                                layer_output = (
                                    all_encoder_layers[int(layer_index)]
                                    .detach()
                                    .cpu()[sentence_index]
                                    )
                            else:
                                layer_output = (
                                    all_encoder_layers[int(layer_index)][sentence_index]
                                    )

                        all_layers.append(layer_output[token_index])

                    if self.use_scalar_mix:
                        sm = ScalarMix(mixture_size=len(all_layers))
                        sm_embeddings = sm(all_layers)
                        all_layers = [sm_embeddings]

                    subtoken_embeddings.append(torch.cat(all_layers))

                # get the current sentence object
                token_idx = 0
                for posidx, token in enumerate(sentence):
                    # add concatenated embedding to sentence
                    token_idx += 1

                    if self.pooling_operation == "first":
                        # use first subword embedding if pooling operation is 'first'
                        token.set_embedding(self.name, subtoken_embeddings[token_idx])
                    else:
                        # otherwise, do a mean over all subwords in token
                        embeddings = subtoken_embeddings[
                            token_idx : token_idx
                            + feature.token_subtoken_count[token.idx]
                        ]
                        embeddings = [
                            embedding.unsqueeze(0) for embedding in embeddings
                        ]
                        mean = torch.mean(torch.cat(embeddings, dim=0), dim=0)
                        token.set_embedding(self.name, mean)

                    token_idx += feature.token_subtoken_count[token.idx] - 1
        if hasattr(sentences, 'features'):
            sentences = self.assign_batch_features(sentences)
        return sentences
    def set_batch_features(self,sentences):
        pass
    @property
    @abstractmethod
    def embedding_length(self) -> int:
        """Returns the length of the embedding vector."""
        return (
            len(self.layer_indexes) * self.model.config.hidden_size
            if not self.use_scalar_mix
            else self.model.config.hidden_size
        )


class CharLMEmbeddings(TokenEmbeddings):
    """Contextual string embeddings of words, as proposed in Akbik et al., 2018. """

    @deprecated(version="0.4", reason="Use 'FlairEmbeddings' instead.")
    def __init__(
        self,
        model: str,
        detach: bool = True,
        use_cache: bool = False,
        cache_directory: Path = None,
    ):
        """
        initializes contextual string embeddings using a character-level language model.
        :param model: model string, one of 'news-forward', 'news-backward', 'news-forward-fast', 'news-backward-fast',
                'mix-forward', 'mix-backward', 'german-forward', 'german-backward', 'polish-backward', 'polish-forward'
                depending on which character language model is desired.
        :param detach: if set to False, the gradient will propagate into the language model. this dramatically slows down
                training and often leads to worse results, so not recommended.
        :param use_cache: if set to False, will not write embeddings to file for later retrieval. this saves disk space but will
                not allow re-use of once computed embeddings that do not fit into memory
        :param cache_directory: if cache_directory is not set, the cache will be written to ~/.flair/embeddings. otherwise the cache
                is written to the provided directory.
        """
        super().__init__()

        cache_dir = Path("embeddings")

        # multilingual forward (English, German, French, Italian, Dutch, Polish)
        if model.lower() == "multi-forward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings-v0.4/lm-multi-forward-v0.1.pt"
            model = cached_path(base_path, cache_dir=cache_dir)
        # multilingual backward  (English, German, French, Italian, Dutch, Polish)
        elif model.lower() == "multi-backward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings-v0.4/lm-multi-backward-v0.1.pt"
            model = cached_path(base_path, cache_dir=cache_dir)

        # news-english-forward
        elif model.lower() == "news-forward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings/lm-news-english-forward-v0.2rc.pt"
            model = cached_path(base_path, cache_dir=cache_dir)

        # news-english-backward
        elif model.lower() == "news-backward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings/lm-news-english-backward-v0.2rc.pt"
            model = cached_path(base_path, cache_dir=cache_dir)

        # news-english-forward
        elif model.lower() == "news-forward-fast":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings/lm-news-english-forward-1024-v0.2rc.pt"
            model = cached_path(base_path, cache_dir=cache_dir)

        # news-english-backward
        elif model.lower() == "news-backward-fast":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings/lm-news-english-backward-1024-v0.2rc.pt"
            model = cached_path(base_path, cache_dir=cache_dir)

        # mix-english-forward
        elif model.lower() == "mix-forward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings/lm-mix-english-forward-v0.2rc.pt"
            model = cached_path(base_path, cache_dir=cache_dir)

        # mix-english-backward
        elif model.lower() == "mix-backward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings/lm-mix-english-backward-v0.2rc.pt"
            model = cached_path(base_path, cache_dir=cache_dir)

        # mix-german-forward
        elif model.lower() == "german-forward" or model.lower() == "de-forward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings/lm-mix-german-forward-v0.2rc.pt"
            model = cached_path(base_path, cache_dir=cache_dir)

        # mix-german-backward
        elif model.lower() == "german-backward" or model.lower() == "de-backward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings/lm-mix-german-backward-v0.2rc.pt"
            model = cached_path(base_path, cache_dir=cache_dir)

        # common crawl Polish forward
        elif model.lower() == "polish-forward" or model.lower() == "pl-forward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings/lm-polish-forward-v0.2.pt"
            model = cached_path(base_path, cache_dir=cache_dir)

        # common crawl Polish backward
        elif model.lower() == "polish-backward" or model.lower() == "pl-backward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings/lm-polish-backward-v0.2.pt"
            model = cached_path(base_path, cache_dir=cache_dir)

        # Slovenian forward
        elif model.lower() == "slovenian-forward" or model.lower() == "sl-forward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings-v0.3/lm-sl-large-forward-v0.1.pt"
            model = cached_path(base_path, cache_dir=cache_dir)
        # Slovenian backward
        elif model.lower() == "slovenian-backward" or model.lower() == "sl-backward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings-v0.3/lm-sl-large-backward-v0.1.pt"
            model = cached_path(base_path, cache_dir=cache_dir)

        # Bulgarian forward
        elif model.lower() == "bulgarian-forward" or model.lower() == "bg-forward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings-v0.3/lm-bg-small-forward-v0.1.pt"
            model = cached_path(base_path, cache_dir=cache_dir)
        # Bulgarian backward
        elif model.lower() == "bulgarian-backward" or model.lower() == "bg-backward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings-v0.3/lm-bg-small-backward-v0.1.pt"
            model = cached_path(base_path, cache_dir=cache_dir)

        # Dutch forward
        elif model.lower() == "dutch-forward" or model.lower() == "nl-forward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings-v0.4/lm-nl-large-forward-v0.1.pt"
            model = cached_path(base_path, cache_dir=cache_dir)
        # Dutch backward
        elif model.lower() == "dutch-backward" or model.lower() == "nl-backward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings-v0.4/lm-nl-large-backward-v0.1.pt"
            model = cached_path(base_path, cache_dir=cache_dir)

        # Swedish forward
        elif model.lower() == "swedish-forward" or model.lower() == "sv-forward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings-v0.4/lm-sv-large-forward-v0.1.pt"
            model = cached_path(base_path, cache_dir=cache_dir)
        # Swedish backward
        elif model.lower() == "swedish-backward" or model.lower() == "sv-backward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings-v0.4/lm-sv-large-backward-v0.1.pt"
            model = cached_path(base_path, cache_dir=cache_dir)

        # French forward
        elif model.lower() == "french-forward" or model.lower() == "fr-forward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings/lm-fr-charlm-forward.pt"
            model = cached_path(base_path, cache_dir=cache_dir)
        # French backward
        elif model.lower() == "french-backward" or model.lower() == "fr-backward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings/lm-fr-charlm-backward.pt"
            model = cached_path(base_path, cache_dir=cache_dir)

        # Czech forward
        elif model.lower() == "czech-forward" or model.lower() == "cs-forward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings-v0.4/lm-cs-large-forward-v0.1.pt"
            model = cached_path(base_path, cache_dir=cache_dir)
        # Czech backward
        elif model.lower() == "czech-backward" or model.lower() == "cs-backward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings-v0.4/lm-cs-large-backward-v0.1.pt"
            model = cached_path(base_path, cache_dir=cache_dir)

        # Portuguese forward
        elif model.lower() == "portuguese-forward" or model.lower() == "pt-forward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings-v0.4/lm-pt-forward.pt"
            model = cached_path(base_path, cache_dir=cache_dir)
        # Portuguese backward
        elif model.lower() == "portuguese-backward" or model.lower() == "pt-backward":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/embeddings-v0.4/lm-pt-backward.pt"
            model = cached_path(base_path, cache_dir=cache_dir)

        elif not Path(model).exists():
            raise ValueError(
                f'The given model "{model}" is not available or is not a valid path.'
            )

        self.name = str(model)
        self.static_embeddings = detach

        from flair.models import LanguageModel

        self.lm = LanguageModel.load_language_model(model)
        self.detach = detach

        self.is_forward_lm: bool = self.lm.is_forward_lm

        # initialize cache if use_cache set
        self.cache = None
        if use_cache:
            cache_path = (
                Path(f"{self.name}-tmp-cache.sqllite")
                if not cache_directory
                else cache_directory / f"{self.name}-tmp-cache.sqllite"
            )
            from sqlitedict import SqliteDict

            self.cache = SqliteDict(str(cache_path), autocommit=True)

        # embed a dummy sentence to determine embedding_length
        dummy_sentence: Sentence = Sentence()
        dummy_sentence.add_token(Token("hello"))
        embedded_dummy = self.embed(dummy_sentence)
        self.__embedding_length: int = len(
            embedded_dummy[0].get_token(1).get_embedding()
        )

        # set to eval mode
        self.eval()

    def train(self, mode=True):
        pass

    def __getstate__(self):
        # Copy the object's state from self.__dict__ which contains
        # all our instance attributes. Always use the dict.copy()
        # method to avoid modifying the original state.
        state = self.__dict__.copy()
        # Remove the unpicklable entries.
        state["cache"] = None
        return state

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def _add_embeddings_internal(self, sentences: List[Sentence]) -> List[Sentence]:

        # if cache is used, try setting embeddings from cache first
        if "cache" in self.__dict__ and self.cache is not None:

            # try populating embeddings from cache
            all_embeddings_retrieved_from_cache: bool = True
            for sentence in sentences:
                key = sentence.to_tokenized_string()
                embeddings = self.cache.get(key)

                if not embeddings:
                    all_embeddings_retrieved_from_cache = False
                    break
                else:
                    for token, embedding in zip(sentence, embeddings):
                        token.set_embedding(self.name, torch.FloatTensor(embedding))

            if all_embeddings_retrieved_from_cache:
                return sentences

        # if this is not possible, use LM to generate embedding. First, get text sentences
        text_sentences = [sentence.to_tokenized_string() for sentence in sentences]

        longest_character_sequence_in_batch: int = len(max(text_sentences, key=len))

        # pad strings with whitespaces to longest sentence
        sentences_padded: List[str] = []
        append_padded_sentence = sentences_padded.append

        end_marker = " "
        extra_offset = 1
        for sentence_text in text_sentences:
            pad_by = longest_character_sequence_in_batch - len(sentence_text)
            if self.is_forward_lm:
                padded = "\n{}{}{}".format(sentence_text, end_marker, pad_by * " ")
                append_padded_sentence(padded)
            else:
                padded = "\n{}{}{}".format(
                    sentence_text[::-1], end_marker, pad_by * " "
                )
                append_padded_sentence(padded)

        # get hidden states from language model
        all_hidden_states_in_lm = self.lm.get_representation(sentences_padded)

        # take first or last hidden states from language model as word representation
        for i, sentence in enumerate(sentences):
            sentence_text = sentence.to_tokenized_string()

            offset_forward: int = extra_offset
            offset_backward: int = len(sentence_text) + extra_offset

            for token in sentence.tokens:

                offset_forward += len(token.text)

                if self.is_forward_lm:
                    offset = offset_forward
                else:
                    offset = offset_backward

                embedding = all_hidden_states_in_lm[offset, i, :]

                # if self.tokenized_lm or token.whitespace_after:
                offset_forward += 1
                offset_backward -= 1

                offset_backward -= len(token.text)

                token.set_embedding(self.name, embedding)

        if "cache" in self.__dict__ and self.cache is not None:
            for sentence in sentences:
                self.cache[sentence.to_tokenized_string()] = [
                    token._embeddings[self.name].tolist() for token in sentence
                ]

        return sentences

    def __str__(self):
        return self.name


class DocumentMeanEmbeddings(DocumentEmbeddings):
    @deprecated(
        version="0.3.1",
        reason="The functionality of this class is moved to 'DocumentPoolEmbeddings'",
    )
    def __init__(self, token_embeddings: List[TokenEmbeddings]):
        """The constructor takes a list of embeddings to be combined."""
        super().__init__()

        self.embeddings: StackedEmbeddings = StackedEmbeddings(
            embeddings=token_embeddings
        )
        self.name: str = "document_mean"

        self.__embedding_length: int = self.embeddings.embedding_length

        self.to(flair.device)

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def embed(self, sentences: Union[List[Sentence], Sentence]):
        """Add embeddings to every sentence in the given list of sentences. If embeddings are already added, updates
        only if embeddings are non-static."""

        everything_embedded: bool = True

        # if only one sentence is passed, convert to list of sentence
        if type(sentences) is Sentence:
            sentences = [sentences]

        for sentence in sentences:
            if self.name not in sentence._embeddings.keys():
                everything_embedded = False

        if not everything_embedded:

            self.embeddings.embed(sentences)

            for sentence in sentences:
                word_embeddings = []
                for token in sentence.tokens:
                    word_embeddings.append(token.get_embedding().unsqueeze(0))

                word_embeddings = torch.cat(word_embeddings, dim=0).to(flair.device)

                mean_embedding = torch.mean(word_embeddings, 0)

                sentence.set_embedding(self.name, mean_embedding)

    def _add_embeddings_internal(self, sentences: List[Sentence]):
        pass


class DocumentPoolEmbeddings(DocumentEmbeddings):
    def __init__(
        self,
        embeddings: List[TokenEmbeddings],
        fine_tune_mode="linear",
        pooling: str = "mean",
    ):
        """The constructor takes a list of embeddings to be combined.
        :param embeddings: a list of token embeddings
        :param pooling: a string which can any value from ['mean', 'max', 'min']
        """
        super().__init__()

        self.embeddings: StackedEmbeddings = StackedEmbeddings(embeddings=embeddings)
        self.__embedding_length = self.embeddings.embedding_length

        # optional fine-tuning on top of embedding layer
        self.fine_tune_mode = fine_tune_mode
        if self.fine_tune_mode in ["nonlinear", "linear"]:
            self.embedding_flex = torch.nn.Linear(
                self.embedding_length, self.embedding_length, bias=False
            )
            self.embedding_flex.weight.data.copy_(torch.eye(self.embedding_length))

        if self.fine_tune_mode in ["nonlinear"]:
            self.embedding_flex_nonlinear = torch.nn.ReLU(self.embedding_length)
            self.embedding_flex_nonlinear_map = torch.nn.Linear(
                self.embedding_length, self.embedding_length
            )

        self.__embedding_length: int = self.embeddings.embedding_length

        self.to(flair.device)

        self.pooling = pooling
        if self.pooling == "mean":
            self.pool_op = torch.mean
        elif pooling == "max":
            self.pool_op = torch.max
        elif pooling == "min":
            self.pool_op = torch.min
        else:
            raise ValueError(f"Pooling operation for {self.mode!r} is not defined")
        self.name: str = f"document_{self.pooling}"

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def embed(self, sentences: Union[List[Sentence], Sentence]):
        """Add embeddings to every sentence in the given list of sentences. If embeddings are already added, updates
        only if embeddings are non-static."""

        # if only one sentence is passed, convert to list of sentence
        if isinstance(sentences, Sentence):
            sentences = [sentences]

        self.embeddings.embed(sentences)

        for sentence in sentences:
            word_embeddings = []
            for token in sentence.tokens:
                word_embeddings.append(token.get_embedding().unsqueeze(0))

            word_embeddings = torch.cat(word_embeddings, dim=0).to(flair.device)

            if self.fine_tune_mode in ["nonlinear", "linear"]:
                word_embeddings = self.embedding_flex(word_embeddings)

            if self.fine_tune_mode in ["nonlinear"]:
                word_embeddings = self.embedding_flex_nonlinear(word_embeddings)
                word_embeddings = self.embedding_flex_nonlinear_map(word_embeddings)

            if self.pooling == "mean":
                pooled_embedding = self.pool_op(word_embeddings, 0)
            else:
                pooled_embedding, _ = self.pool_op(word_embeddings, 0)

            sentence.set_embedding(self.name, pooled_embedding)

    def _add_embeddings_internal(self, sentences: List[Sentence]):
        pass

    def extra_repr(self):
        return f"fine_tune_mode={self.fine_tune_mode}, pooling={self.pooling}"


class DocumentRNNEmbeddings(DocumentEmbeddings):
    def __init__(
        self,
        embeddings: List[TokenEmbeddings],
        hidden_size=128,
        rnn_layers=1,
        reproject_words: bool = True,
        reproject_words_dimension: int = None,
        bidirectional: bool = False,
        dropout: float = 0.5,
        word_dropout: float = 0.0,
        locked_dropout: float = 0.0,
        rnn_type="GRU",
    ):
        """The constructor takes a list of embeddings to be combined.
        :param embeddings: a list of token embeddings
        :param hidden_size: the number of hidden states in the rnn
        :param rnn_layers: the number of layers for the rnn
        :param reproject_words: boolean value, indicating whether to reproject the token embeddings in a separate linear
        layer before putting them into the rnn or not
        :param reproject_words_dimension: output dimension of reprojecting token embeddings. If None the same output
        dimension as before will be taken.
        :param bidirectional: boolean value, indicating whether to use a bidirectional rnn or not
        :param dropout: the dropout value to be used
        :param word_dropout: the word dropout value to be used, if 0.0 word dropout is not used
        :param locked_dropout: the locked dropout value to be used, if 0.0 locked dropout is not used
        :param rnn_type: 'GRU' or 'LSTM'
        """
        super().__init__()

        self.embeddings: StackedEmbeddings = StackedEmbeddings(embeddings=embeddings)

        self.rnn_type = rnn_type

        self.reproject_words = reproject_words
        self.bidirectional = bidirectional

        self.length_of_all_token_embeddings: int = self.embeddings.embedding_length

        self.static_embeddings = False

        self.__embedding_length: int = hidden_size
        if self.bidirectional:
            self.__embedding_length *= 4

        self.embeddings_dimension: int = self.length_of_all_token_embeddings
        if self.reproject_words and reproject_words_dimension is not None:
            self.embeddings_dimension = reproject_words_dimension

        self.word_reprojection_map = torch.nn.Linear(
            self.length_of_all_token_embeddings, self.embeddings_dimension
        )

        # bidirectional RNN on top of embedding layer
        if rnn_type == "LSTM":
            self.rnn = torch.nn.LSTM(
                self.embeddings_dimension,
                hidden_size,
                num_layers=rnn_layers,
                bidirectional=self.bidirectional,
            )
        else:
            self.rnn = torch.nn.GRU(
                self.embeddings_dimension,
                hidden_size,
                num_layers=rnn_layers,
                bidirectional=self.bidirectional,
            )

        self.name = "document_" + self.rnn._get_name()

        # dropouts
        if locked_dropout > 0.0:
            self.dropout: torch.nn.Module = LockedDropout(locked_dropout)
        else:
            self.dropout = torch.nn.Dropout(dropout)

        self.use_word_dropout: bool = word_dropout > 0.0
        if self.use_word_dropout:
            self.word_dropout = WordDropout(word_dropout)

        torch.nn.init.xavier_uniform_(self.word_reprojection_map.weight)

        self.to(flair.device)

        self.eval()

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def embed(self, sentences: Union[List[Sentence], Sentence]):
        """Add embeddings to all sentences in the given list of sentences. If embeddings are already added, update
         only if embeddings are non-static."""

        if type(sentences) is Sentence:
            sentences = [sentences]

        self.rnn.zero_grad()

        # the permutation that sorts the sentences by length, descending
        sort_perm = np.argsort([len(s) for s in sentences])[::-1]

        # the inverse permutation that restores the input order; it's an index tensor therefore LongTensor
        sort_invperm = np.argsort(sort_perm)

        # sort sentences by number of tokens
        sentences = [sentences[i] for i in sort_perm]

        self.embeddings.embed(sentences)

        longest_token_sequence_in_batch: int = len(sentences[0])

        # all_sentence_tensors = []
        lengths: List[int] = []

        # initialize zero-padded word embeddings tensor
        sentence_tensor = torch.zeros(
            [
                len(sentences),
                longest_token_sequence_in_batch,
                self.embeddings.embedding_length,
            ],
            dtype=torch.float,
            device=flair.device,
        )

        # fill values with word embeddings
        for s_id, sentence in enumerate(sentences):
            lengths.append(len(sentence.tokens))

            sentence_tensor[s_id][: len(sentence)] = torch.cat(
                [token.get_embedding().unsqueeze(0) for token in sentence], 0
            )

        # TODO: this can only be removed once the implementations of word_dropout and locked_dropout have a batch_first mode
        sentence_tensor = sentence_tensor.transpose_(0, 1)

        # --------------------------------------------------------------------
        # FF PART
        # --------------------------------------------------------------------
        # use word dropout if set
        if self.use_word_dropout:
            sentence_tensor = self.word_dropout(sentence_tensor)

        if self.reproject_words:
            sentence_tensor = self.word_reprojection_map(sentence_tensor)

        sentence_tensor = self.dropout(sentence_tensor)
        packed = pack_padded_sequence(sentence_tensor, lengths)

        self.rnn.flatten_parameters()

        rnn_out, hidden = self.rnn(packed)

        outputs, output_lengths = pad_packed_sequence(rnn_out)

        outputs = self.dropout(outputs)

        # --------------------------------------------------------------------
        # EXTRACT EMBEDDINGS FROM RNN
        # --------------------------------------------------------------------
        for sentence_no, length in enumerate(lengths):
            last_rep = outputs[length - 1, sentence_no]

            embedding = last_rep
            if self.bidirectional:
                first_rep = outputs[0, sentence_no]
                embedding = torch.cat([first_rep, last_rep], 0)

            sentence = sentences[sentence_no]
            sentence.set_embedding(self.name, embedding)

        # restore original order of sentences in the batch
        sentences = [sentences[i] for i in sort_invperm]

    def _add_embeddings_internal(self, sentences: List[Sentence]):
        pass


@deprecated(
    version="0.4",
    reason="The functionality of this class is moved to 'DocumentRNNEmbeddings'",
)
class DocumentLSTMEmbeddings(DocumentEmbeddings):
    def __init__(
        self,
        embeddings: List[TokenEmbeddings],
        hidden_size=128,
        rnn_layers=1,
        reproject_words: bool = True,
        reproject_words_dimension: int = None,
        bidirectional: bool = False,
        dropout: float = 0.5,
        word_dropout: float = 0.0,
        locked_dropout: float = 0.0,
    ):
        """The constructor takes a list of embeddings to be combined.
        :param embeddings: a list of token embeddings
        :param hidden_size: the number of hidden states in the lstm
        :param rnn_layers: the number of layers for the lstm
        :param reproject_words: boolean value, indicating whether to reproject the token embeddings in a separate linear
        layer before putting them into the lstm or not
        :param reproject_words_dimension: output dimension of reprojecting token embeddings. If None the same output
        dimension as before will be taken.
        :param bidirectional: boolean value, indicating whether to use a bidirectional lstm or not
        :param dropout: the dropout value to be used
        :param word_dropout: the word dropout value to be used, if 0.0 word dropout is not used
        :param locked_dropout: the locked dropout value to be used, if 0.0 locked dropout is not used
        """
        super().__init__()

        self.embeddings: StackedEmbeddings = StackedEmbeddings(embeddings=embeddings)

        self.reproject_words = reproject_words
        self.bidirectional = bidirectional

        self.length_of_all_token_embeddings: int = self.embeddings.embedding_length

        self.name = "document_lstm"
        self.static_embeddings = False

        self.__embedding_length: int = hidden_size
        if self.bidirectional:
            self.__embedding_length *= 4

        self.embeddings_dimension: int = self.length_of_all_token_embeddings
        if self.reproject_words and reproject_words_dimension is not None:
            self.embeddings_dimension = reproject_words_dimension

        # bidirectional LSTM on top of embedding layer
        self.word_reprojection_map = torch.nn.Linear(
            self.length_of_all_token_embeddings, self.embeddings_dimension
        )
        self.rnn = torch.nn.GRU(
            self.embeddings_dimension,
            hidden_size,
            num_layers=rnn_layers,
            bidirectional=self.bidirectional,
        )

        # dropouts
        if locked_dropout > 0.0:
            self.dropout: torch.nn.Module = LockedDropout(locked_dropout)
        else:
            self.dropout = torch.nn.Dropout(dropout)

        self.use_word_dropout: bool = word_dropout > 0.0
        if self.use_word_dropout:
            self.word_dropout = WordDropout(word_dropout)

        torch.nn.init.xavier_uniform_(self.word_reprojection_map.weight)

        self.to(flair.device)

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def embed(self, sentences: Union[List[Sentence], Sentence]):
        """Add embeddings to all sentences in the given list of sentences. If embeddings are already added, update
         only if embeddings are non-static."""

        if type(sentences) is Sentence:
            sentences = [sentences]

        self.rnn.zero_grad()

        sentences.sort(key=lambda x: len(x), reverse=True)

        self.embeddings.embed(sentences)

        # first, sort sentences by number of tokens
        longest_token_sequence_in_batch: int = len(sentences[0])

        all_sentence_tensors = []
        lengths: List[int] = []

        # go through each sentence in batch
        for i, sentence in enumerate(sentences):

            lengths.append(len(sentence.tokens))

            word_embeddings = []

            for token, token_idx in zip(sentence.tokens, range(len(sentence.tokens))):
                word_embeddings.append(token.get_embedding().unsqueeze(0))

            # PADDING: pad shorter sentences out
            for add in range(longest_token_sequence_in_batch - len(sentence.tokens)):
                word_embeddings.append(
                    torch.zeros(
                        self.length_of_all_token_embeddings, dtype=torch.float
                    ).unsqueeze(0)
                )

            word_embeddings_tensor = torch.cat(word_embeddings, 0).to(flair.device)

            sentence_states = word_embeddings_tensor

            # ADD TO SENTENCE LIST: add the representation
            all_sentence_tensors.append(sentence_states.unsqueeze(1))

        # --------------------------------------------------------------------
        # GET REPRESENTATION FOR ENTIRE BATCH
        # --------------------------------------------------------------------
        sentence_tensor = torch.cat(all_sentence_tensors, 1)

        # --------------------------------------------------------------------
        # FF PART
        # --------------------------------------------------------------------
        # use word dropout if set
        if self.use_word_dropout:
            sentence_tensor = self.word_dropout(sentence_tensor)

        if self.reproject_words:
            sentence_tensor = self.word_reprojection_map(sentence_tensor)

        sentence_tensor = self.dropout(sentence_tensor)

        packed = torch.nn.utils.rnn.pack_padded_sequence(sentence_tensor, lengths)

        self.rnn.flatten_parameters()

        lstm_out, hidden = self.rnn(packed)

        outputs, output_lengths = torch.nn.utils.rnn.pad_packed_sequence(lstm_out)

        outputs = self.dropout(outputs)

        # --------------------------------------------------------------------
        # EXTRACT EMBEDDINGS FROM LSTM
        # --------------------------------------------------------------------
        for sentence_no, length in enumerate(lengths):
            last_rep = outputs[length - 1, sentence_no]

            embedding = last_rep
            if self.bidirectional:
                first_rep = outputs[0, sentence_no]
                embedding = torch.cat([first_rep, last_rep], 0)

            sentence = sentences[sentence_no]
            sentence.set_embedding(self.name, embedding)

    def _add_embeddings_internal(self, sentences: List[Sentence]):
        pass


class DocumentLMEmbeddings(DocumentEmbeddings):
    def __init__(self, flair_embeddings: List[FlairEmbeddings]):
        super().__init__()

        self.embeddings = flair_embeddings
        self.name = "document_lm"

        # IMPORTANT: add embeddings as torch modules
        for i, embedding in enumerate(flair_embeddings):
            self.add_module("lm_embedding_{}".format(i), embedding)
            if not embedding.static_embeddings:
                self.static_embeddings = False

        self._embedding_length: int = sum(
            embedding.embedding_length for embedding in flair_embeddings
        )

    @property
    def embedding_length(self) -> int:
        return self._embedding_length

    def _add_embeddings_internal(self, sentences: List[Sentence]):
        if type(sentences) is Sentence:
            sentences = [sentences]

        for embedding in self.embeddings:
            embedding.embed(sentences)

            # iterate over sentences
            for sentence in sentences:
                sentence: Sentence = sentence

                # if its a forward LM, take last state
                if embedding.is_forward_lm:
                    sentence.set_embedding(
                        embedding.name,
                        sentence[len(sentence) - 1]._embeddings[embedding.name],
                    )
                else:
                    sentence.set_embedding(
                        embedding.name, sentence[0]._embeddings[embedding.name]
                    )

        return sentences


class NILCEmbeddings(WordEmbeddings):
    def __init__(self, embeddings: str, model: str = "skip", size: int = 100):
        """
        Initializes portuguese classic word embeddings trained by NILC Lab (http://www.nilc.icmc.usp.br/embeddings).
        Constructor downloads required files if not there.
        :param embeddings: one of: 'fasttext', 'glove', 'wang2vec' or 'word2vec'
        :param model: one of: 'skip' or 'cbow'. This is not applicable to glove.
        :param size: one of: 50, 100, 300, 600 or 1000.
        """

        base_path = "http://143.107.183.175:22980/download.php?file=embeddings/"

        cache_dir = Path("embeddings") / embeddings.lower()

        # GLOVE embeddings
        if embeddings.lower() == "glove":
            cached_path(
                f"{base_path}{embeddings}/{embeddings}_s{size}.zip", cache_dir=cache_dir
            )
            embeddings = cached_path(
                f"{base_path}{embeddings}/{embeddings}_s{size}.zip", cache_dir=cache_dir
            )

        elif embeddings.lower() in ["fasttext", "wang2vec", "word2vec"]:
            cached_path(
                f"{base_path}{embeddings}/{model}_s{size}.zip", cache_dir=cache_dir
            )
            embeddings = cached_path(
                f"{base_path}{embeddings}/{model}_s{size}.zip", cache_dir=cache_dir
            )

        elif not Path(embeddings).exists():
            raise ValueError(
                f'The given embeddings "{embeddings}" is not available or is not a valid path.'
            )

        self.name: str = str(embeddings)
        self.static_embeddings = True

        log.info("Reading embeddings from %s" % embeddings)
        self.precomputed_word_embeddings = gensim.models.KeyedVectors.load_word2vec_format(
            open_inside_zip(str(embeddings), cache_dir=cache_dir)
        )

        self.__embedding_length: int = self.precomputed_word_embeddings.vector_size
        super(TokenEmbeddings, self).__init__()

    @property
    def embedding_length(self) -> int:
        return self.__embedding_length

    def __str__(self):
        return self.name


def replace_with_language_code(string: str):
    string = string.replace("arabic-", "ar-")
    string = string.replace("basque-", "eu-")
    string = string.replace("bulgarian-", "bg-")
    string = string.replace("croatian-", "hr-")
    string = string.replace("czech-", "cs-")
    string = string.replace("danish-", "da-")
    string = string.replace("dutch-", "nl-")
    string = string.replace("farsi-", "fa-")
    string = string.replace("persian-", "fa-")
    string = string.replace("finnish-", "fi-")
    string = string.replace("french-", "fr-")
    string = string.replace("german-", "de-")
    string = string.replace("hebrew-", "he-")
    string = string.replace("hindi-", "hi-")
    string = string.replace("indonesian-", "id-")
    string = string.replace("italian-", "it-")
    string = string.replace("japanese-", "ja-")
    string = string.replace("norwegian-", "no")
    string = string.replace("polish-", "pl-")
    string = string.replace("portuguese-", "pt-")
    string = string.replace("slovenian-", "sl-")
    string = string.replace("spanish-", "es-")
    string = string.replace("swedish-", "sv-")
    return string
