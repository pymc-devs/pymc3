#   Copyright 2021 The PyMC Developers
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""
Created on Mar 7, 2011

@author: johnsalvatier
"""
import aesara
import aesara.tensor as aet
import numpy as np
import scipy.linalg
import scipy.stats

from aesara import scan
from aesara.compile.builders import OpFromGraph
from aesara.graph.basic import Apply
from aesara.graph.op import Op
from aesara.scalar import UnaryScalarOp, upgrade_to_float_no_complex
from aesara.scan import until
from aesara.tensor.elemwise import Elemwise
from aesara.tensor.slinalg import Cholesky, Solve

from pymc3.aesaraf import floatX
from pymc3.distributions.shape_utils import to_tuple
from pymc3.distributions.special import gammaln
from pymc3.model import modelcontext

f = floatX
c = -0.5 * np.log(2.0 * np.pi)
_beta_clip_values = {
    dtype: (np.nextafter(0, 1, dtype=dtype), np.nextafter(1, 0, dtype=dtype))
    for dtype in ["float16", "float32", "float64"]
}


def bound(logp, *conditions, **kwargs):
    """
    Bounds a log probability density with several conditions.

    Parameters
    ----------
    logp: float
    *conditions: booleans
    broadcast_conditions: bool (optional, default=True)
        If True, broadcasts logp to match the largest shape of the conditions.
        This is used e.g. in DiscreteUniform where logp is a scalar constant and the shape
        is specified via the conditions.
        If False, will return the same shape as logp.
        This is used e.g. in Multinomial where broadcasting can lead to differences in the logp.

    Returns
    -------
    logp with elements set to -inf where any condition is False
    """

    # If called inside a model context, see if bounds check is disabled
    try:
        model = modelcontext(kwargs.get("model"))
        if not model.check_bounds:
            return logp
    except TypeError:  # No model found
        pass

    broadcast_conditions = kwargs.get("broadcast_conditions", True)

    if broadcast_conditions:
        alltrue = alltrue_elemwise
    else:
        alltrue = alltrue_scalar

    return aet.switch(alltrue(conditions), logp, -np.inf)


def alltrue_elemwise(vals):
    ret = 1
    for c in vals:
        ret = ret * (1 * c)
    return ret


def alltrue_scalar(vals):
    return aet.all([aet.all(1 * val) for val in vals])


def logpow(x, m):
    """
    Calculates log(x**m) since m*log(x) will fail when m, x = 0.
    """
    # return m * log(x)
    return aet.switch(aet.eq(x, 0), aet.switch(aet.eq(m, 0), 0.0, -np.inf), m * aet.log(x))


def factln(n):
    return gammaln(n + 1)


def binomln(n, k):
    return factln(n) - factln(k) - factln(n - k)


def betaln(x, y):
    return gammaln(x) + gammaln(y) - gammaln(x + y)


def std_cdf(x):
    """
    Calculates the standard normal cumulative distribution function.
    """
    return 0.5 + 0.5 * aet.erf(x / aet.sqrt(2.0))


def normal_lcdf(mu, sigma, x):
    """Compute the log of the cumulative density function of the normal."""
    z = (x - mu) / sigma
    return aet.switch(
        aet.lt(z, -1.0),
        aet.log(aet.erfcx(-z / aet.sqrt(2.0)) / 2.0) - aet.sqr(z) / 2.0,
        aet.log1p(-aet.erfc(z / aet.sqrt(2.0)) / 2.0),
    )


def normal_lccdf(mu, sigma, x):
    z = (x - mu) / sigma
    return aet.switch(
        aet.gt(z, 1.0),
        aet.log(aet.erfcx(z / aet.sqrt(2.0)) / 2.0) - aet.sqr(z) / 2.0,
        aet.log1p(-aet.erfc(-z / aet.sqrt(2.0)) / 2.0),
    )


def log_diff_normal_cdf(mu, sigma, x, y):
    """
    Compute :math:`\\log(\\Phi(\frac{x - \\mu}{\\sigma}) - \\Phi(\frac{y - \\mu}{\\sigma}))` safely in log space.

    Parameters
    ----------
    mu: float
        mean
    sigma: float
        std

    x: float

    y: float
        must be strictly less than x.

    Returns
    -------
    log (\\Phi(x) - \\Phi(y))

    """
    x = (x - mu) / sigma / aet.sqrt(2.0)
    y = (y - mu) / sigma / aet.sqrt(2.0)

    # To stabilize the computation, consider these three regions:
    # 1) x > y > 0 => Use erf(x) = 1 - e^{-x^2} erfcx(x) and erf(y) =1 - e^{-y^2} erfcx(y)
    # 2) 0 > x > y => Use erf(x) = e^{-x^2} erfcx(-x) and erf(y) = e^{-y^2} erfcx(-y)
    # 3) x > 0 > y => Naive formula log( (erf(x) - erf(y)) / 2 ) works fine.
    return aet.log(0.5) + aet.switch(
        aet.gt(y, 0),
        -aet.square(y)
        + aet.log(aet.erfcx(y) - aet.exp(aet.square(y) - aet.square(x)) * aet.erfcx(x)),
        aet.switch(
            aet.lt(x, 0),  # 0 > x > y
            -aet.square(x)
            + aet.log(aet.erfcx(-x) - aet.exp(aet.square(x) - aet.square(y)) * aet.erfcx(-y)),
            aet.log(aet.erf(x) - aet.erf(y)),  # x >0 > y
        ),
    )


def sigma2rho(sigma):
    """
    `sigma -> rho` aesara converter
    :math:`mu + sigma*e = mu + log(1+exp(rho))*e`"""
    return aet.log(aet.exp(aet.abs_(sigma)) - 1.0)


def rho2sigma(rho):
    """
    `rho -> sigma` aesara converter
    :math:`mu + sigma*e = mu + log(1+exp(rho))*e`"""
    return aet.nnet.softplus(rho)


rho2sd = rho2sigma
sd2rho = sigma2rho


def log_normal(x, mean, **kwargs):
    """
    Calculate logarithm of normal distribution at point `x`
    with given `mean` and `std`

    Parameters
    ----------
    x: Tensor
        point of evaluation
    mean: Tensor
        mean of normal distribution
    kwargs: one of parameters `{sigma, tau, w, rho}`

    Notes
    -----
    There are four variants for density parametrization.
    They are:
        1) standard deviation - `std`
        2) `w`, logarithm of `std` :math:`w = log(std)`
        3) `rho` that follows this equation :math:`rho = log(exp(std) - 1)`
        4) `tau` that follows this equation :math:`tau = std^{-1}`
    ----
    """
    sigma = kwargs.get("sigma")
    w = kwargs.get("w")
    rho = kwargs.get("rho")
    tau = kwargs.get("tau")
    eps = kwargs.get("eps", 0.0)
    check = sum(map(lambda a: a is not None, [sigma, w, rho, tau]))
    if check > 1:
        raise ValueError("more than one required kwarg is passed")
    if check == 0:
        raise ValueError("none of required kwarg is passed")
    if sigma is not None:
        std = sigma
    elif w is not None:
        std = aet.exp(w)
    elif rho is not None:
        std = rho2sigma(rho)
    else:
        std = tau ** (-1)
    std += f(eps)
    return f(c) - aet.log(aet.abs_(std)) - (x - mean) ** 2 / (2.0 * std ** 2)


def MvNormalLogp():
    """Compute the log pdf of a multivariate normal distribution.

    This should be used in MvNormal.logp once Theano#5908 is released.

    Parameters
    ----------
    cov: aet.matrix
        The covariance matrix.
    delta: aet.matrix
        Array of deviations from the mean.
    """
    cov = aet.matrix("cov")
    cov.tag.test_value = floatX(np.eye(3))
    delta = aet.matrix("delta")
    delta.tag.test_value = floatX(np.zeros((2, 3)))

    solve_lower = Solve(A_structure="lower_triangular")
    solve_upper = Solve(A_structure="upper_triangular")
    cholesky = Cholesky(lower=True, on_error="nan")

    n, k = delta.shape
    n, k = f(n), f(k)
    chol_cov = cholesky(cov)
    diag = aet.diag(chol_cov)
    ok = aet.all(diag > 0)

    chol_cov = aet.switch(ok, chol_cov, aet.fill(chol_cov, 1))
    delta_trans = solve_lower(chol_cov, delta.T).T

    result = n * k * aet.log(f(2) * np.pi)
    result += f(2) * n * aet.sum(aet.log(diag))
    result += (delta_trans ** f(2)).sum()
    result = f(-0.5) * result
    logp = aet.switch(ok, result, -np.inf)

    def dlogp(inputs, gradients):
        (g_logp,) = gradients
        cov, delta = inputs

        g_logp.tag.test_value = floatX(1.0)
        n, k = delta.shape

        chol_cov = cholesky(cov)
        diag = aet.diag(chol_cov)
        ok = aet.all(diag > 0)

        chol_cov = aet.switch(ok, chol_cov, aet.fill(chol_cov, 1))
        delta_trans = solve_lower(chol_cov, delta.T).T

        inner = n * aet.eye(k) - aet.dot(delta_trans.T, delta_trans)
        g_cov = solve_upper(chol_cov.T, inner)
        g_cov = solve_upper(chol_cov.T, g_cov.T)

        tau_delta = solve_upper(chol_cov.T, delta_trans.T)
        g_delta = tau_delta.T

        g_cov = aet.switch(ok, g_cov, -np.nan)
        g_delta = aet.switch(ok, g_delta, -np.nan)

        return [-0.5 * g_cov * g_logp, -g_delta * g_logp]

    return OpFromGraph([cov, delta], [logp], grad_overrides=dlogp, inline=True)


class SplineWrapper(Op):
    """
    Creates a aesara operation from scipy.interpolate.UnivariateSpline
    """

    __props__ = ("spline",)

    def __init__(self, spline):
        self.spline = spline

    def make_node(self, x):
        x = aet.as_tensor_variable(x)
        return Apply(self, [x], [x.type()])

    @property
    def grad_op(self):
        if not hasattr(self, "_grad_op"):
            try:
                self._grad_op = SplineWrapper(self.spline.derivative())
            except ValueError:
                self._grad_op = None

        if self._grad_op is None:
            raise NotImplementedError("Spline of order 0 is not differentiable")
        return self._grad_op

    def perform(self, node, inputs, output_storage):
        (x,) = inputs
        output_storage[0][0] = np.asarray(self.spline(x))

    def grad(self, inputs, grads):
        (x,) = inputs
        (x_grad,) = grads

        return [x_grad * self.grad_op(x)]


class I1e(UnaryScalarOp):
    """
    Modified Bessel function of the first kind of order 1, exponentially scaled.
    """

    nfunc_spec = ("scipy.special.i1e", 1, 1)

    def impl(self, x):
        return scipy.special.i1e(x)


i1e_scalar = I1e(upgrade_to_float_no_complex, name="i1e")
i1e = Elemwise(i1e_scalar, name="Elemwise{i1e,no_inplace}")


class I0e(UnaryScalarOp):
    """
    Modified Bessel function of the first kind of order 0, exponentially scaled.
    """

    nfunc_spec = ("scipy.special.i0e", 1, 1)

    def impl(self, x):
        return scipy.special.i0e(x)

    def grad(self, inp, grads):
        (x,) = inp
        (gz,) = grads
        return (gz * (i1e_scalar(x) - aesara.scalar.sgn(x) * i0e_scalar(x)),)


i0e_scalar = I0e(upgrade_to_float_no_complex, name="i0e")
i0e = Elemwise(i0e_scalar, name="Elemwise{i0e,no_inplace}")


def random_choice(*args, **kwargs):
    """Return draws from a categorial probability functions

    Args:
        p: array
           Probability of each class. If p.ndim > 1, the last axis is
           interpreted as the probability of each class, and numpy.random.choice
           is iterated for every other axis element.
        size: int or tuple
            Shape of the desired output array. If p is multidimensional, size
            should broadcast with p.shape[:-1].

    Returns:
        random sample: array

    """
    p = kwargs.pop("p")
    size = kwargs.pop("size")
    k = p.shape[-1]

    if p.ndim > 1:
        # If p is an nd-array, the last axis is interpreted as the class
        # probability. We must iterate over the elements of all the other
        # dimensions.
        # We first ensure that p is broadcasted to the output's shape
        size = to_tuple(size) + (1,)
        p = np.broadcast_arrays(p, np.empty(size))[0]
        out_shape = p.shape[:-1]
        # np.random.choice accepts 1D p arrays, so we semiflatten p to
        # iterate calls using the last axis as the category probabilities
        p = np.reshape(p, (-1, p.shape[-1]))
        samples = np.array([np.random.choice(k, p=p_) for p_ in p])
        # We reshape to the desired output shape
        samples = np.reshape(samples, out_shape)
    else:
        samples = np.random.choice(k, p=p, size=size)
    return samples


def zvalue(value, sigma, mu):
    """
    Calculate the z-value for a normal distribution.
    """
    return (value - mu) / sigma


def incomplete_beta_cfe(a, b, x, small):
    """Incomplete beta continued fraction expansions
    based on Cephes library by Steve Moshier (incbet.c).
    small: Choose element-wise which continued fraction expansion to use.
    """
    BIG = aet.constant(4.503599627370496e15, dtype="float64")
    BIGINV = aet.constant(2.22044604925031308085e-16, dtype="float64")
    THRESH = aet.constant(3.0 * np.MachAr().eps, dtype="float64")

    zero = aet.constant(0.0, dtype="float64")
    one = aet.constant(1.0, dtype="float64")
    two = aet.constant(2.0, dtype="float64")

    r = one
    k1 = a
    k3 = a
    k4 = a + one
    k5 = one
    k8 = a + two

    k2 = aet.switch(small, a + b, b - one)
    k6 = aet.switch(small, b - one, a + b)
    k7 = aet.switch(small, k4, a + one)
    k26update = aet.switch(small, one, -one)
    x = aet.switch(small, x, x / (one - x))

    pkm2 = zero
    qkm2 = one
    pkm1 = one
    qkm1 = one
    r = one

    def _step(i, pkm1, pkm2, qkm1, qkm2, k1, k2, k3, k4, k5, k6, k7, k8, r):
        xk = -(x * k1 * k2) / (k3 * k4)
        pk = pkm1 + pkm2 * xk
        qk = qkm1 + qkm2 * xk
        pkm2 = pkm1
        pkm1 = pk
        qkm2 = qkm1
        qkm1 = qk

        xk = (x * k5 * k6) / (k7 * k8)
        pk = pkm1 + pkm2 * xk
        qk = qkm1 + qkm2 * xk
        pkm2 = pkm1
        pkm1 = pk
        qkm2 = qkm1
        qkm1 = qk

        old_r = r
        r = aet.switch(aet.eq(qk, zero), r, pk / qk)

        k1 += one
        k2 += k26update
        k3 += two
        k4 += two
        k5 += one
        k6 -= k26update
        k7 += two
        k8 += two

        big_cond = aet.gt(aet.abs_(qk) + aet.abs_(pk), BIG)
        biginv_cond = aet.or_(aet.lt(aet.abs_(qk), BIGINV), aet.lt(aet.abs_(pk), BIGINV))

        pkm2 = aet.switch(big_cond, pkm2 * BIGINV, pkm2)
        pkm1 = aet.switch(big_cond, pkm1 * BIGINV, pkm1)
        qkm2 = aet.switch(big_cond, qkm2 * BIGINV, qkm2)
        qkm1 = aet.switch(big_cond, qkm1 * BIGINV, qkm1)

        pkm2 = aet.switch(biginv_cond, pkm2 * BIG, pkm2)
        pkm1 = aet.switch(biginv_cond, pkm1 * BIG, pkm1)
        qkm2 = aet.switch(biginv_cond, qkm2 * BIG, qkm2)
        qkm1 = aet.switch(biginv_cond, qkm1 * BIG, qkm1)

        return (
            (pkm1, pkm2, qkm1, qkm2, k1, k2, k3, k4, k5, k6, k7, k8, r),
            until(aet.abs_(old_r - r) < (THRESH * aet.abs_(r))),
        )

    (pkm1, pkm2, qkm1, qkm2, k1, k2, k3, k4, k5, k6, k7, k8, r), _ = scan(
        _step,
        sequences=[aet.arange(0, 300)],
        outputs_info=[
            e
            for e in aet.cast(
                (pkm1, pkm2, qkm1, qkm2, k1, k2, k3, k4, k5, k6, k7, k8, r), "float64"
            )
        ],
    )

    return r[-1]


def incomplete_beta_ps(a, b, value):
    """Power series for incomplete beta
    Use when b*x is small and value not too close to 1.
    Based on Cephes library by Steve Moshier (incbet.c)
    """
    one = aet.constant(1, dtype="float64")
    ai = one / a
    u = (one - b) * value
    t1 = u / (a + one)
    t = u
    threshold = np.MachAr().eps * ai
    s = aet.constant(0, dtype="float64")

    def _step(i, t, s):
        t *= (i - b) * value / i
        step = t / (a + i)
        s += step
        return ((t, s), until(aet.abs_(step) < threshold))

    (t, s), _ = scan(
        _step, sequences=[aet.arange(2, 302)], outputs_info=[e for e in aet.cast((t, s), "float64")]
    )

    s = s[-1] + t1 + ai

    t = gammaln(a + b) - gammaln(a) - gammaln(b) + a * aet.log(value) + aet.log(s)
    return aet.exp(t)


def incomplete_beta(a, b, value):
    """Incomplete beta implementation
    Power series and continued fraction expansions chosen for best numerical
    convergence across the board based on inputs.
    """
    machep = aet.constant(np.MachAr().eps, dtype="float64")
    one = aet.constant(1, dtype="float64")
    w = one - value

    ps = incomplete_beta_ps(a, b, value)

    flip = aet.gt(value, (a / (a + b)))
    aa, bb = a, b
    a = aet.switch(flip, bb, aa)
    b = aet.switch(flip, aa, bb)
    xc = aet.switch(flip, value, w)
    x = aet.switch(flip, w, value)

    tps = incomplete_beta_ps(a, b, x)
    tps = aet.switch(aet.le(tps, machep), one - machep, one - tps)

    # Choose which continued fraction expansion for best convergence.
    small = aet.lt(x * (a + b - 2.0) - (a - one), 0.0)
    cfe = incomplete_beta_cfe(a, b, x, small)
    w = aet.switch(small, cfe, cfe / xc)

    # Direct incomplete beta accounting for flipped a, b.
    t = aet.exp(
        a * aet.log(x) + b * aet.log(xc) + gammaln(a + b) - gammaln(a) - gammaln(b) + aet.log(w / a)
    )

    t = aet.switch(flip, aet.switch(aet.le(t, machep), one - machep, one - t), t)
    return aet.switch(
        aet.and_(flip, aet.and_(aet.le((b * x), one), aet.le(x, 0.95))),
        tps,
        aet.switch(aet.and_(aet.le(b * value, one), aet.le(value, 0.95)), ps, t),
    )


def clipped_beta_rvs(a, b, size=None, dtype="float64"):
    """Draw beta distributed random samples in the open :math:`(0, 1)` interval.

    The samples are generated with ``scipy.stats.beta.rvs``, but any value that
    is equal to 0 or 1 will be shifted towards the next floating point in the
    interval :math:`[0, 1]`, depending on the floating point precision that is
    given by ``dtype``.

    Parameters
    ----------
    a : float or array_like of floats
        Alpha, strictly positive (>0).
    b : float or array_like of floats
        Beta, strictly positive (>0).
    size : int or tuple of ints, optional
        Output shape.  If the given shape is, e.g., ``(m, n, k)``, then
        ``m * n * k`` samples are drawn.  If size is ``None`` (default),
        a single value is returned if ``a`` and ``b`` are both scalars.
        Otherwise, ``np.broadcast(a, b).size`` samples are drawn.
    dtype : str or dtype instance
        The floating point precision that the samples should have. This also
        determines the value that will be used to shift any samples returned
        by the numpy random number generator that are zero or one.

    Returns
    -------
    out : ndarray or scalar
        Drawn samples from the parameterized beta distribution. The scipy
        implementation can yield values that are equal to zero or one. We
        assume the support of the Beta distribution to be in the open interval
        :math:`(0, 1)`, so we shift any sample that is equal to 0 to
        ``np.nextafter(0, 1, dtype=dtype)`` and any sample that is equal to 1
        is shifted to ``np.nextafter(1, 0, dtype=dtype)``.

    """
    out = scipy.stats.beta.rvs(a, b, size=size).astype(dtype)
    lower, upper = _beta_clip_values[dtype]
    return np.maximum(np.minimum(out, upper), lower)
