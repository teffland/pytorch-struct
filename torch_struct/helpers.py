import torch
import math
from .semirings import LogSemiring
from torch.autograd import Function


class Get(torch.autograd.Function):
    """Torch function used by `Chart` to differentiably access values."""

    @staticmethod
    def forward(ctx, chart, grad_chart, indices):
        ctx.save_for_backward(grad_chart)
        out = chart[indices]
        ctx.indices = indices
        return out

    @staticmethod
    def backward(ctx, grad_output):
        (grad_chart,) = ctx.saved_tensors
        grad_chart[ctx.indices] += grad_output
        return grad_chart, None, None


class Set(torch.autograd.Function):
    """Torch function used by `Chart` to differentiably set values."""

    @staticmethod
    def forward(ctx, chart, indices, vals):
        chart[indices] = vals
        ctx.indices = indices
        return chart

    @staticmethod
    def backward(ctx, grad_output):
        z = grad_output[ctx.indices]
        return None, None, z


class Chart:
    """Represents a tabular data structure used by dynamic programs.

    Provides differentiable getters and setters for the chart that are
    automatically broadcaset along semiring, sample, and batch dimensions.
    """

    def __init__(self, size, potentials, semiring, cache=True):
        self.data = semiring.zero_(
            torch.zeros(*((semiring.size(),) + size), dtype=potentials.dtype, device=potentials.device)
        )
        self.grad = self.data.detach().clone().fill_(0.0)
        self.cache = cache

    def __getitem__(self, ind):
        I = slice(None)
        if self.cache:
            return Get.apply(self.data, self.grad, (I, I) + ind)
        else:
            return self.data[(I, I) + ind]

    def __setitem__(self, ind, new):
        I = slice(None)
        if self.cache:
            self.data = Set.apply(self.data, (I, I) + ind, new)
        else:
            self.data[(I, I) + ind] = new

    def get(self, ind):
        return Get.apply(self.data, self.grad, ind)

    def set(self, ind, new):
        self.data = Set.apply(self.data, ind, new)


class _Struct:
    """`_Struct` is base class used to represent the graphical structure of a model.

    Subclasses should implement a `_dp` method which computes the partition function (under the standard `_BaseSemiring`).
    Different `StructDistribution` methods will instantiate the `_Struct` subclasses
    """

    def __init__(self, semiring=LogSemiring):
        self.semiring = semiring

    def _dp(self, scores, lengths=None, force_grad=False, cache=True):
        """Implement computation equivalent to the computing partition constant Z (if self.semiring == `_BaseSemiring`).

        Params:
          scores: torch.FloatTensor, log potential scores for each factor of the model. Shape (* x batch size x *event_shape )
          lengths: torch.LongTensor = None, lengths of batch padded examples. Shape = ( * x batch size )
          force_grad: bool = False
          cache: bool = True

        Returns:
          v: torch.Tensor, the resulting output of the dynammic program
          edges: List[torch.Tensor], the log edge potentials of the model.
                 When `scores` is already in a log_potential format for the distribution (typical), this will be
                 [scores], as in `Alignment`, `LinearChain`, `SemiMarkov`, `CKY_CRF`.
                 An exceptional case is the `CKY` struct, which takes log potential parameters from production rules
                 for a PCFG, which are by definition independent of position in the sequence.
          charts: Optional[List[Chart]] = None, the charts used in computing the dp. They are needed if we want to run the
                  "backward" dynamic program and compute things like marginals w/o autograd.

        """
        raise NotImplementedError

    def score(self, potentials, parts, batch_dims=[0]):
        """Score for entire structure is product of potentials for all activated "parts"."""
        score = torch.mul(potentials, parts)  # mask potentials by activated "parts"
        batch = tuple((score.shape[b] for b in batch_dims))
        return self.semiring.prod(score.view(batch + (-1,)))  # product of all potentialsa

    def _bin_length(self, length):
        """Find least upper bound for lengths that is a power of 2. Used in parallel scans."""
        log_N = int(math.ceil(math.log(length, 2)))
        bin_N = int(math.pow(2, log_N))
        return log_N, bin_N

    def _get_dimension(self, edge):
        if isinstance(edge, list):
            for t in edge:
                t.requires_grad_(True)
            return edge[0].shape
        else:
            edge.requires_grad_(True)
            return edge.shape

    def _chart(self, size, potentials, force_grad):
        return self._make_chart(1, size, potentials, force_grad)[0]

    def _make_chart(self, N, size, potentials, force_grad=False):
        return [
            (
                self.semiring.zero_(
                    torch.zeros(*((self.semiring.size(),) + size), dtype=potentials.dtype, device=potentials.device)
                ).requires_grad_(force_grad and not potentials.requires_grad)
            )
            for _ in range(N)
        ]

    def sum(self, edge, lengths=None, _autograd=True, _raw=False):
        """
        Compute the (semiring) sum over all structures model.

        Parameters:
            params : generic params (see class)
            lengths: None or b long tensor mask

        Returns:
            v: b tensor of total sum
        """

        if _autograd or self.semiring is not LogSemiring or not hasattr(self, "_dp_backward"):

            v = self._dp(edge, lengths)[0]
            if _raw:
                return v
            return self.semiring.unconvert(v)

        else:
            v, _, alpha = self._dp(edge, lengths, False)

            class DPManual(Function):
                @staticmethod
                def forward(ctx, input):
                    return v

                @staticmethod
                def backward(ctx, grad_v):
                    marginals = self._dp_backward(edge, lengths, alpha)
                    return marginals.mul(grad_v.view((grad_v.shape[0],) + tuple([1] * marginals.dim())))

            return DPManual.apply(edge)

    def marginals(self, edge, lengths=None, _autograd=True, _raw=False):
        """
        Compute the marginals of a structured model.

        Parameters:
            params : generic params (see class)
            lengths: None or b long tensor mask
        Returns:
            marginals: b x (N-1) x C x C table

        """
        if _autograd or self.semiring is not LogSemiring or not hasattr(self, "_dp_backward"):
            with torch.enable_grad():
                v, edges, _ = self._dp(edge, lengths=lengths, force_grad=True, cache=not _raw)
                if _raw:
                    all_m = []
                    for k in range(v.shape[0]):
                        obj = v[k].sum(dim=0)

                        marg = torch.autograd.grad(
                            obj,
                            edges,
                            create_graph=True,
                            only_inputs=True,
                            allow_unused=False,
                        )
                        all_m.append(self.semiring.unconvert(self._arrange_marginals(marg)))
                    return torch.stack(all_m, dim=0)
                else:
                    obj = self.semiring.unconvert(v).sum(dim=0)
                    # print("Getting marginals with autograd")
                    marg = torch.autograd.grad(obj, edges, create_graph=True, only_inputs=True, allow_unused=False)
                    # print("done")
                    a_m = self._arrange_marginals(marg)
                    return self.semiring.unconvert(a_m)
        else:
            v, _, alpha = self._dp(edge, lengths=lengths, force_grad=True)
            return self._dp_backward(edge, lengths, alpha)

    @staticmethod
    def to_parts(spans, extra, lengths=None):
        return spans

    @staticmethod
    def from_parts(spans):
        return spans, None

    def _arrange_marginals(self, marg):
        return marg[0]

    # For Testing
    def _rand(self, *args, **kwargs):
        """TODO:"""
        raise NotImplementedError

    def enumerate(self, edge, lengths=None):
        """TODO:"""
        raise NotImplementedError
